"""הרצה: python -m cpa_ai  (ברירת מחדל http://127.0.0.1:8000)"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("cpa_ai.api:create_app", factory=True,
                host=os.getenv("CPA_AI_HOST", "127.0.0.1"), port=int(os.getenv("CPA_AI_PORT", "8000")))
