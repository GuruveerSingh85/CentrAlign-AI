
import json
import os
from typing import TypedDict

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END

load_dotenv()


class AgentState(TypedDict):
    objective: str
    plan: dict


def get_model():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is missing from backend/.env")

    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        google_api_key=api_key,
        temperature=0,
    )


def understand_and_plan(state: AgentState):
    model = get_model()

    prompt = f"""
You are the planning component of CentrAlign AI,
an autonomous company operator.

Interpret the user's objective and produce a practical,
safe plan. Do not claim that any action has been executed.

Return ONLY valid JSON with this structure:
{{
  "goal": "the intended outcome",
  "success_criteria": ["observable condition"],
  "steps": [
    {{
      "step": 1,
      "action": "what to do",
      "capability": "read_company_policy | browse_website | check_policy | register_vendor",
      "requires_approval": false,
      "expected_result": "what evidence to look for"
    }}
  ],
  "missing_information": [],
  "risks": []
}}

Only choose capabilities from the listed set.
Require human approval before creating or changing business records.
If important information is missing, identify it instead of inventing it.

User objective:
{state["objective"]}
"""

    response = model.invoke(prompt)
    content = response.content

    if isinstance(content, list):
        content = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )

    content = str(content).strip()

    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0].strip()

    plan = json.loads(content)

    if not isinstance(plan.get("steps"), list):
        raise ValueError("The model returned an invalid plan.")

    return {"plan": plan}


builder = StateGraph(AgentState)

builder.add_node("understand_and_plan", understand_and_plan)

builder.add_edge(START, "understand_and_plan")
builder.add_edge("understand_and_plan", END)

planner_graph = builder.compile()


def create_plan(objective: str) -> dict:
    result = planner_graph.invoke({
        "objective": objective,
        "plan": {},
    })
    return result["plan"]
