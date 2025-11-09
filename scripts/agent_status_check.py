import argparse
from datetime import datetime, timedelta
from typing import Callable, Dict

from langgraph.graph import END, StateGraph

from src.agents.state import AgentState
from src.agents.market_data import market_data_agent
from src.agents.technicals import technical_analyst_agent
from src.agents.fundamentals import fundamentals_agent
from src.agents.sentiment import sentiment_agent
from src.agents.valuation import valuation_agent
from src.agents.macro_news_agent import macro_news_agent
from src.agents.researcher_bull import researcher_bull_agent
from src.agents.researcher_bear import researcher_bear_agent
from src.agents.debate_room import debate_room_agent
from src.agents.risk_manager import risk_management_agent
from src.agents.macro_analyst import macro_analyst_agent
from src.agents.portfolio_manager import portfolio_management_agent

AgentFunc = Callable[[AgentState], AgentState]
AGENT_NAMES = [
    "market_data_agent",
    "technical_analyst_agent",
    "fundamentals_agent",
    "sentiment_agent",
    "valuation_agent",
    "macro_news_agent",
    "researcher_bull_agent",
    "researcher_bear_agent",
    "debate_room_agent",
    "risk_management_agent",
    "macro_analyst_agent",
    "portfolio_management_agent",
]
AGENT_FUNCS: Dict[str, AgentFunc] = {
    "market_data_agent": market_data_agent,
    "technical_analyst_agent": technical_analyst_agent,
    "fundamentals_agent": fundamentals_agent,
    "sentiment_agent": sentiment_agent,
    "valuation_agent": valuation_agent,
    "macro_news_agent": macro_news_agent,
    "researcher_bull_agent": researcher_bull_agent,
    "researcher_bear_agent": researcher_bear_agent,
    "debate_room_agent": debate_room_agent,
    "risk_management_agent": risk_management_agent,
    "macro_analyst_agent": macro_analyst_agent,
    "portfolio_management_agent": portfolio_management_agent,
}


def _wrap_agent(name: str, func: AgentFunc, status: Dict[str, Dict[str, str]]) -> AgentFunc:
    def wrapper(state: AgentState) -> AgentState:
        status[name] = {"status": "running"}
        print(f"[START] {name}")
        try:
            result = func(state)
            status[name] = {"status": "success"}
            print(f"[OK] {name}")
            return result
        except Exception as exc:  # noqa: BLE001
            status[name] = {"status": "failed", "error": str(exc)}
            print(f"[ERROR] {name}: {exc}")
            raise
    return wrapper


def build_instrumented_app(status: Dict[str, Dict[str, str]]):
    workflow = StateGraph(AgentState)
    for name in AGENT_NAMES:
        workflow.add_node(name, _wrap_agent(name, AGENT_FUNCS[name], status))

    workflow.set_entry_point("market_data_agent")
    workflow.add_edge("market_data_agent", "technical_analyst_agent")
    workflow.add_edge("market_data_agent", "fundamentals_agent")
    workflow.add_edge("market_data_agent", "sentiment_agent")
    workflow.add_edge("market_data_agent", "valuation_agent")
    workflow.add_edge("market_data_agent", "macro_news_agent")

    workflow.add_edge("technical_analyst_agent", "researcher_bull_agent")
    workflow.add_edge("fundamentals_agent", "researcher_bull_agent")
    workflow.add_edge("sentiment_agent", "researcher_bull_agent")
    workflow.add_edge("valuation_agent", "researcher_bull_agent")

    workflow.add_edge("technical_analyst_agent", "researcher_bear_agent")
    workflow.add_edge("fundamentals_agent", "researcher_bear_agent")
    workflow.add_edge("sentiment_agent", "researcher_bear_agent")
    workflow.add_edge("valuation_agent", "researcher_bear_agent")

    workflow.add_edge("researcher_bull_agent", "debate_room_agent")
    workflow.add_edge("researcher_bear_agent", "debate_room_agent")

    workflow.add_edge("debate_room_agent", "risk_management_agent")
    workflow.add_edge("risk_management_agent", "macro_analyst_agent")
    workflow.add_edge("macro_news_agent", "macro_analyst_agent")
    workflow.add_edge("macro_analyst_agent", "portfolio_management_agent")
    workflow.add_edge("portfolio_management_agent", END)
    return workflow.compile()


def parse_args():
    parser = argparse.ArgumentParser(description="Agent status diagnostic runner")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--show-reasoning", action="store_true")
    parser.add_argument("--num-of-news", type=int, default=20)
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--initial-position", type=int, default=0)
    return parser.parse_args()


def main():
    args = parse_args()
    current_date = datetime.now()
    yesterday = current_date - timedelta(days=1)
    end_date = yesterday if not args.end_date else min(datetime.strptime(args.end_date, "%Y-%m-%d"), yesterday)
    start_date = (
        end_date - timedelta(days=365)
        if not args.start_date
        else datetime.strptime(args.start_date, "%Y-%m-%d")
    )

    initial_state: AgentState = {
        "messages": [],
        "data": {
            "ticker": args.ticker,
            "portfolio": {"cash": args.initial_capital, "stock": args.initial_position},
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "num_of_news": args.num_of_news,
        },
        "metadata": {
            "show_reasoning": args.show_reasoning,
            "run_id": "diagnostic",
            "show_summary": False,
        },
    }

    status: Dict[str, Dict[str, str]] = {}
    app = build_instrumented_app(status)
    try:
        final_state = app.invoke(initial_state)
        print("\n[RESULT] Workflow completed. Final message snippet:")
        final_msg = final_state["messages"][-1].content if final_state.get("messages") else "<no message>"
        print(final_msg[:500])
    except Exception as exc:  # noqa: BLE001
        print(f"\n[RESULT] Workflow raised exception: {exc}")
    finally:
        from pathlib import Path
        import json

        print("\nAgent Status Summary:")
        for name in AGENT_NAMES:
            info = status.get(name, {"status": "skipped"})
            suffix = f" - {info['error']}" if info.get("error") else ""
            print(f"- {name}: {info['status']}{suffix}")
        summary_path = Path("logs/agent_status_summary.json")
        summary_path.write_text(json.dumps(status, ensure_ascii=False, indent=2))
        print(f"\nDetailed status written to {summary_path}")


if __name__ == "__main__":
    main()
