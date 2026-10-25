import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Optional operator overrides; providers fall back to their verified defaults.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()
PROVIDER = os.getenv("NAVIGATOR_PROVIDER", "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "360"))

# Screenshots are local-only by default. Remote vision is opt-in because a
# rendered page can contain PII that never appears in the DOM snapshot.
ALLOW_SCREENSHOTS = os.getenv("ALLOW_SCREENSHOTS", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_INSTRUCTION_CHARS = int(os.getenv("MAX_INSTRUCTION_CHARS", "4000"))
