from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

import pandas as pd

from src.tools.api import get_price_data


@dataclass(frozen=True)
class BacktestConfig:
    ticker: str
    start_date: str
    end_date: str
    initial_capital: float = 100000.0
    num_of_news: int = 5
    mode: str = "technical"  # "technical" | "workflow"
    decision_interval: int = 5  # only for workflow mode
    plot: bool = False
    save_plot_path: str | None = None


class Backtester:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        agent: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.portfolio: Dict[str, Any] = {"cash": config.initial_capital, "stock": 0}
        self.portfolio_values: list[Dict[str, Any]] = []

        self._api_call_count = 0
        self._api_window_start = time.time()
        self._last_api_call = 0.0

        self.logger = self._setup_logging()
        self._setup_backtest_logging()
        self._validate_inputs()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("backtester")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def _setup_backtest_logging(self) -> None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(log_dir, exist_ok=True)

        self.backtest_logger = logging.getLogger("backtest")
        self.backtest_logger.setLevel(logging.INFO)
        if self.backtest_logger.handlers:
            self.backtest_logger.handlers.clear()

        current_date = datetime.now().strftime("%Y%m%d")
        period = f"{self.config.start_date.replace('-', '')}_{self.config.end_date.replace('-', '')}"
        log_file = os.path.join(log_dir, f"backtest_{self.config.ticker}_{current_date}_{period}.log")

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        self.backtest_logger.addHandler(file_handler)

        self.backtest_logger.info(f"回测开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.backtest_logger.info(f"股票代码: {self.config.ticker}")
        self.backtest_logger.info(f"回测区间: {self.config.start_date} 至 {self.config.end_date}")
        self.backtest_logger.info(f"初始资金: {self.config.initial_capital:,.2f}")
        self.backtest_logger.info(f"模式: {self.config.mode}")
        self.backtest_logger.info("-" * 100)

    def _validate_inputs(self) -> None:
        start = datetime.strptime(self.config.start_date, "%Y-%m-%d")
        end = datetime.strptime(self.config.end_date, "%Y-%m-%d")
        if start >= end:
            raise ValueError("开始日期必须早于结束日期")
        if self.config.initial_capital <= 0:
            raise ValueError("初始资金必须大于 0")
        if not isinstance(self.config.ticker, str) or len(self.config.ticker) != 6:
            raise ValueError("无效的股票代码格式（应为 6 位字符串）")
        if self.config.mode not in {"technical", "workflow"}:
            raise ValueError("mode must be one of: technical, workflow")
        if self.config.mode == "workflow" and self.agent is None:
            raise ValueError("workflow mode requires agent (e.g. run_hedge_fund)")

    def _rate_limit(self) -> None:
        # 重置窗口
        now = time.time()
        if now - self._api_window_start >= 60:
            self._api_call_count = 0
            self._api_window_start = now

        # 简单的窗口限流（避免跑 workflow 时打爆接口）
        if self._api_call_count >= 8:
            wait_time = 60 - (now - self._api_window_start)
            if wait_time > 0:
                time.sleep(wait_time)
            self._api_call_count = 0
            self._api_window_start = time.time()

        if self._last_api_call:
            delta = time.time() - self._last_api_call
            if delta < 6:
                time.sleep(6 - delta)

        self._last_api_call = time.time()
        self._api_call_count += 1

    def _get_agent_decision(self, current_date: str, lookback_start: str) -> Dict[str, Any]:
        if self.agent is None:
            return {"decision": {"action": "hold", "quantity": 0}, "analyst_signals": {}}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._rate_limit()
                result = self.agent(
                    ticker=self.config.ticker,
                    start_date=lookback_start,
                    end_date=current_date,
                    portfolio=self.portfolio,
                    num_of_news=self.config.num_of_news,
                    run_id=f"backtest_{self.config.ticker}_{current_date.replace('-', '')}",
                )
                # run_hedge_fund 返回文本；这里做一个兼容包装，避免 backtester 崩溃
                if isinstance(result, dict):
                    return result
                return {"decision": self._parse_decision_from_text(str(result)), "analyst_signals": {}}
            except Exception as e:  # noqa: BLE001
                self.logger.warning("获取智能体决策失败(尝试 %s/%s): %s", attempt + 1, max_retries, e)
                time.sleep(2**attempt)
        return {"decision": {"action": "hold", "quantity": 0}, "analyst_signals": {}}

    @staticmethod
    def _parse_decision_from_text(text: str) -> Dict[str, Any]:
        text = (text or "").lower()
        decision = {"action": "hold", "quantity": 0}
        if "buy" in text or "bullish" in text:
            decision["action"] = "buy"
            decision["quantity"] = 100
        elif "sell" in text or "bearish" in text:
            decision["action"] = "sell"
            decision["quantity"] = 100
        return decision

    def _execute_trade(self, action: str, quantity: int, current_price: float) -> int:
        if current_price <= 0:
            return 0
        if action == "buy" and quantity > 0:
            cost = quantity * current_price
            if cost <= self.portfolio["cash"]:
                self.portfolio["stock"] += quantity
                self.portfolio["cash"] -= cost
                return quantity
            max_qty = int(self.portfolio["cash"] // current_price)
            if max_qty > 0:
                self.portfolio["stock"] += max_qty
                self.portfolio["cash"] -= max_qty * current_price
                return max_qty
            return 0
        if action == "sell" and quantity > 0:
            quantity = min(quantity, int(self.portfolio["stock"] or 0))
            if quantity > 0:
                self.portfolio["cash"] += quantity * current_price
                self.portfolio["stock"] -= quantity
                return quantity
        return 0

    def run_backtest(self) -> None:
        dates = pd.date_range(self.config.start_date, self.config.end_date, freq="B")
        if dates.empty:
            self.logger.warning("No business days in range, nothing to backtest.")
            return

        self.logger.info("开始回测: %s (%s)", self.config.ticker, self.config.mode)
        print(
            f"{'日期':<12} {'代码':<6} {'操作':<6} {'数量':>8} {'价格':>10} "
            f"{'现金':>12} {'持仓':>8} {'总值':>14} {'日收益%':>10}"
        )
        print("-" * 100)

        prefetch_start = (dates[0] - timedelta(days=200)).strftime("%Y-%m-%d")
        prefetch_end = dates[-1].strftime("%Y-%m-%d")
        price_df = get_price_data(self.config.ticker, prefetch_start, prefetch_end)
        if price_df is None or price_df.empty or "date" not in price_df.columns:
            self.logger.error("Failed to load price data for backtest.")
            return

        price_df = price_df.copy()
        price_df["date"] = pd.to_datetime(price_df["date"])
        price_df = price_df.sort_values("date")

        last_decision: Dict[str, Any] = {"action": "hold", "quantity": 0}

        for idx, current_date in enumerate(dates):
            current_date_str = current_date.strftime("%Y-%m-%d")
            day_rows = price_df.loc[price_df["date"] == pd.to_datetime(current_date_str)]
            if day_rows.empty:
                continue
            current_price = float(day_rows.iloc[-1].get("open", 0.0) or 0.0)

            action = "hold"
            quantity = 0
            output: Dict[str, Any] = {}

            if self.config.mode == "technical":
                hist = price_df.loc[price_df["date"] <= pd.to_datetime(current_date_str)]
                close_series = pd.to_numeric(hist.get("close"), errors="coerce").dropna()
                if len(close_series) >= 20:
                    ma20 = float(close_series.tail(20).mean())
                    last_close = float(close_series.iloc[-1])
                    if last_close > ma20 and self.portfolio["stock"] == 0:
                        action = "buy"
                        quantity = 100
                    elif last_close < ma20 and self.portfolio["stock"] > 0:
                        action = "sell"
                        quantity = int(self.portfolio["stock"])
            else:
                if idx % max(1, int(self.config.decision_interval)) == 0:
                    lookback_start = (current_date - timedelta(days=30)).strftime("%Y-%m-%d")
                    output = self._get_agent_decision(current_date_str, lookback_start)
                    last_decision = output.get("decision", last_decision) or last_decision
                action = str(last_decision.get("action", "hold") or "hold")
                quantity = int(last_decision.get("quantity", 0) or 0)

            self.backtest_logger.info(f"\n交易日期: {current_date_str}")
            if self.config.mode == "workflow" and output.get("analyst_signals"):
                self.backtest_logger.info("\n各智能体分析结果:")
                for agent_name, signal in output["analyst_signals"].items():
                    self.backtest_logger.info(f"\n{agent_name}: {signal}")
            self.backtest_logger.info(f"行动: {action.upper()}")
            self.backtest_logger.info(f"数量: {quantity}")

            executed_qty = self._execute_trade(action, quantity, current_price)

            total_value = float(self.portfolio["cash"]) + float(self.portfolio["stock"]) * current_price
            self.portfolio["portfolio_value"] = total_value

            if self.portfolio_values:
                daily_return = (total_value / self.portfolio_values[-1]["Portfolio Value"] - 1) * 100
            else:
                daily_return = 0.0

            print(
                f"{current_date_str:<12} {self.config.ticker:<6} {action:<6} {executed_qty:>8} "
                f"{current_price:>10.2f} {self.portfolio['cash']:>12.2f} {self.portfolio['stock']:>8} "
                f"{total_value:>14.2f} {daily_return:>10.2f}"
            )

            self.portfolio_values.append(
                {"Date": current_date, "Portfolio Value": total_value, "Daily Return": daily_return}
            )

    def analyze_performance(self) -> pd.DataFrame:
        performance_df = pd.DataFrame(self.portfolio_values).set_index("Date")
        if performance_df.empty:
            self.logger.warning("No performance data to analyze.")
            return performance_df

        performance_df["Cumulative Return"] = (
            performance_df["Portfolio Value"] / float(self.config.initial_capital) - 1
        ) * 100

        total_return = (
            float(self.portfolio.get("portfolio_value", self.config.initial_capital)) - float(self.config.initial_capital)
        ) / float(self.config.initial_capital)
        print(f"\n总收益率: {total_return * 100:.2f}%")

        self.backtest_logger.info("\n" + "=" * 50)
        self.backtest_logger.info("回测结果汇总")
        self.backtest_logger.info("=" * 50)
        self.backtest_logger.info(f"初始资金: {self.config.initial_capital:,.2f}")
        self.backtest_logger.info(f"最终总值: {self.portfolio.get('portfolio_value', 0):,.2f}")
        self.backtest_logger.info(f"总收益率: {total_return * 100:.2f}%")

        # 可选绘图：默认不 show（避免无 GUI 环境卡死）
        if self.config.plot or self.config.save_plot_path:
            try:
                import matplotlib
                import matplotlib.pyplot as plt

                if os.name == "nt":
                    matplotlib.rc("font", family="Microsoft YaHei")
                matplotlib.rcParams["axes.unicode_minus"] = False

                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))
                performance_df["Portfolio Value (K)"] = performance_df["Portfolio Value"] / 1000.0

                ax1.plot(performance_df.index, performance_df["Portfolio Value (K)"], color="blue", marker="o")
                ax1.set_ylabel("组合价值(千元)")
                ax1.set_title("组合价值变化")
                ax1.grid(True)

                ax2.plot(performance_df.index, performance_df["Cumulative Return"], color="green", marker="o")
                ax2.set_ylabel("累计收益率(%)")
                ax2.set_title("累计收益率变化")
                ax2.grid(True)
                plt.tight_layout()

                if self.config.save_plot_path:
                    os.makedirs(os.path.dirname(self.config.save_plot_path), exist_ok=True)
                    plt.savefig(self.config.save_plot_path, dpi=150)
                    self.logger.info("Saved backtest plot to %s", self.config.save_plot_path)
                if self.config.plot:
                    plt.show()
                plt.close(fig)
            except Exception as plot_err:  # noqa: BLE001
                self.logger.warning("Plot skipped due to error: %s", plot_err)

        return performance_df


def _parse_args() -> BacktestConfig:
    import argparse

    parser = argparse.ArgumentParser(description="运行回测模拟")
    parser.add_argument("--ticker", type=str, required=True, help="股票代码 (例如: 600519)")
    parser.add_argument(
        "--end-date",
        type=str,
        default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
        help="结束日期 YYYY-MM-DD（默认昨天）",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=(datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
        help="开始日期 YYYY-MM-DD（默认 90 天前）",
    )
    parser.add_argument("--initial-capital", type=float, default=100000.0, help="初始资金 (默认: 100000)")
    parser.add_argument("--num-of-news", type=int, default=5, help="workflow 模式下每次用多少条新闻 (默认: 5)")
    parser.add_argument(
        "--mode",
        type=str,
        default="technical",
        choices=["technical", "workflow"],
        help="technical=快速技术回测(不调用LLM/工作流)，workflow=慢速回测(调用完整工作流)",
    )
    parser.add_argument(
        "--decision-interval",
        type=int,
        default=5,
        help="workflow 模式下每 N 个交易日运行一次工作流（默认 5）",
    )
    parser.add_argument("--plot", action="store_true", help="展示图表（可能阻塞），默认不展示")
    parser.add_argument("--save-plot", type=str, default="", help="保存图表到路径（默认不保存）")

    args = parser.parse_args()
    save_plot_path = args.save_plot or None
    if save_plot_path and not os.path.isabs(save_plot_path):
        save_plot_path = os.path.join(os.getcwd(), save_plot_path)

    return BacktestConfig(
        ticker=args.ticker,
        start_date=args.start_date,
        end_date=args.end_date,
        initial_capital=args.initial_capital,
        num_of_news=args.num_of_news,
        mode=args.mode,
        decision_interval=args.decision_interval,
        plot=bool(args.plot),
        save_plot_path=save_plot_path,
    )


if __name__ == "__main__":
    cfg = _parse_args()
    agent = None
    if cfg.mode == "workflow":
        from src.main import run_hedge_fund  # lazy import: avoid main side-effects in technical mode

        agent = run_hedge_fund

    backtester = Backtester(cfg, agent=agent)
    backtester.run_backtest()
    backtester.analyze_performance()
