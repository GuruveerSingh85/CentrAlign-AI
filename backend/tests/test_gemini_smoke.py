import os
import unittest
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.agent import create_plan
from app.workflow import _validated_plan


@unittest.skipUnless(
    os.getenv("RUN_GEMINI_SMOKE") == "1",
    "Set RUN_GEMINI_SMOKE=1 to call the configured Gemini model.",
)
class GeminiPlannerSmokeTests(unittest.TestCase):
    def test_configured_model_returns_a_supported_policy_plan(self):
        if not os.getenv("GEMINI_API_KEY"):
            self.skipTest("GEMINI_API_KEY is not configured.")

        objective = "Review the current procurement policy and its vendor safeguards. Do not register a vendor."
        planner_plan = create_plan(objective)
        validated_plan, workflow_name = _validated_plan(objective, planner_plan)

        self.assertEqual(workflow_name, "procurement_policy_review")
        self.assertTrue(validated_plan["goal"].strip())
        self.assertTrue(validated_plan["steps"])
        self.assertTrue(all(
            step["capability"] == "read_company_policy"
            for step in validated_plan["steps"]
        ))


if __name__ == "__main__":
    unittest.main()
