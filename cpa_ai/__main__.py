"""הרצה: python -m cpa_ai  (ברירת מחדל http://127.0.0.1:8000)"""
import os

import sys

import uvicorn

if __name__ == "__main__":
    if not (os.getenv("CPA_AI_API_KEY") or os.getenv("CPA_AI_USERS") or os.getenv("CPA_AI_ALLOW_INSECURE") == "1"):
        sys.exit("הגדירו CPA_AI_API_KEY (או CPA_AI_USERS). לפיתוח מקומי בלבד: CPA_AI_ALLOW_INSECURE=1")
    uvicorn.run("cpa_ai.api:create_app", factory=True,
                host=os.getenv("CPA_AI_HOST", "127.0.0.1"), port=int(os.getenv("CPA_AI_PORT", "8000")))
