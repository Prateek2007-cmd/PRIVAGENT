import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
PROVIDER = os.getenv("NAVIGATOR_PROVIDER", "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "360"))
