"""One-line smoke test: generate a plan for a search task with the real key.
Usage: venv/Scripts/python scripts/check_gemini.py
Reads GEMINI_API_KEY from backend/.env. Prints the plan or the strict error.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from dotenv import load_dotenv

load_dotenv()

from app.contracts import BrowserObservation, InteractiveElement
from app.planner import plan_from_observation
from app.privacy import classify_and_redact
from app.providers.gemini import GeminiProvider

key = os.getenv("GEMINI_API_KEY", "")
if not key:
    sys.exit("no GEMINI_API_KEY in backend/.env")

obs = BrowserObservation.model_validate({
    "tabId": 0, "url": "https://www.google.com/", "title": "Google",
    "interactive": [
        InteractiveElement.model_validate({"targetId": "qbox", "role": "textbox", "name": "Search", "tag": "input", "inputType": "text", "visible": True, "enabled": True}),
        InteractiveElement.model_validate({"targetId": "qbtn", "role": "button", "name": "Google Search", "tag": "button", "visible": True, "enabled": True}),
    ],
    "scroll": {"y": 0, "viewport": {"w": 1200, "h": 800}}, "dpr": 1,
})
payload, _ = classify_and_redact(obs, instruction="search for news")
provider = GeminiProvider(api_key=key)
try:
    plan = plan_from_observation(instruction="Search Google for the latest news about the James Webb Space Telescope", payload=payload, provider=provider)
    print(json.dumps(plan.model_dump(by_alias=True), indent=2))
except Exception as exc:
    print(f"GEMINI SMOKE TEST FAILED: {exc}", file=sys.stderr)
    sys.exit(1)
