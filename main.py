"""Conference Agent — extract conference data from CMT or EasyChair Smart CFP.

  Extract from EasyChair:  python main.py --source easychair
  Extract from CMT:        python main.py --source cmt
  One-shot LLM agent:      python main.py ask "extract upcoming conferences from easychair"
  Interactive agent:        python main.py chat
"""

import sys
from pathlib import Path


def main():
    try:
        from agent.cli import main as cli_main
    except ModuleNotFoundError as exc:
        requirements = Path(__file__).resolve().parent / "requirements.txt"
        sys.exit(
            f"Missing package '{exc.name}'. Install with:\n"
            f'  "{sys.executable}" -m pip install -r "{requirements}"\n'
            f'  "{sys.executable}" -m playwright install chromium'
        )

    sys.exit(cli_main() or 0)


if __name__ == "__main__":
    main()
