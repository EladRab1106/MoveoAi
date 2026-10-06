"""Ask the agent from the command line:  python -m scripts.ask "your question" """

import json
import sys

from app.agent.agent import run_agent
from app.agent.schemas import ChatMessage

if __name__ == "__main__":
    resp = run_agent([ChatMessage(role="user", content=" ".join(sys.argv[1:]))])
    print(json.dumps(resp.model_dump(), indent=2))
