from __future__ import annotations

# Usage:
# uv run python src/backtester.py --ticker 002074 --start-date 2025-10-01 --end-date 2025-11-15
#
# Args:
# --ticker           股票代码（6 位）
# --start-date       回测开始日期 YYYY-MM-DD
# --end-date         回测结束日期 YYYY-MM-DD
# --num-of-news      每次调用工作流使用的新闻数量
# --decision-interval 每 N 个交易日运行一次工作流
# --plot             展示图表（可选，可能阻塞）
# --save-plot         保存图表路径（可选，不填则保存至 logs/backtest_* 目录）
# 配置项:
# config.json -> backtest.force_run=true 时，禁用回测决策缓存并在日志目录后添加 _forceN

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional
import sys

import pandas as pd

from src.tools.api import get_price_data
from src.database import AkshareSQLiteCache
from src.tools.akshare_cache import CACHE_PATH


# 统一控制台编码，避免 Windows 下 emoji/中文导致的 gbk 编码异常
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

@dataclass(frozen=True)
class BacktestConfig:
    ticker: str
    start_date: str
    end_date: str
    initial_mode: str = "cash"  # cash | shares
    initial_capital: float = 100000.0
    initial_position: int = 0
    force_run: bool = False
    num_of_news: int = 5
    decision_interval: int = 1  # run workflow every N business days
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
        if config.initial_mode == "shares":
            self.portfolio: Dict[str, Any] = {"cash": 0.0, "stock": 0}
        else:
            self.portfolio = {"cash": config.initial_capital, "stock": 0}
        self.portfolio_values: list[Dict[str, Any]] = []
        self._initial_position_applied = False

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
        log_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(log_root, exist_ok=True)

        self.backtest_logger = logging.getLogger("backtest")
        self.backtest_logger.setLevel(logging.INFO)
        if self.backtest_logger.handlers:
            self.backtest_logger.handlers.clear()

        current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        period = f"{self.config.start_date.replace('-', '')}_{self.config.end_date.replace('-', '')}"
        run_dir = os.path.join(log_root, f"backtest_{self.config.ticker}_{current_date}_{period}")
        if self.config.force_run:
            suffix = 1
            candidate = f"{run_dir}_force{suffix}"
            while os.path.exists(candidate):
                suffix += 1
                candidate = f"{run_dir}_force{suffix}"
            run_dir = candidate
        os.makedirs(run_dir, exist_ok=True)

        self._backtest_run_dir = run_dir
        log_file = os.path.join(run_dir, "backtest.log")
        self._decision_log_path = os.path.join(run_dir, "llm_decisions.json")

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        self.backtest_logger.addHandler(file_handler)

        self.backtest_logger.info(f"回测开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.backtest_logger.info(f"股票代码: {self.config.ticker}")
        self.backtest_logger.info(f"回测区间: {self.config.start_date} 至 {self.config.end_date}")
        self.backtest_logger.info(f"初始模式: {self.config.initial_mode}")
        self.backtest_logger.info(f"初始资金: {self.config.initial_capital:,.2f}")
        self.backtest_logger.info(f"初始持仓: {self.config.initial_position} 股")
        self.backtest_logger.info(f"强制运行: {self.config.force_run}")
        self.backtest_logger.info("模式: workflow")
        self.backtest_logger.info("-" * 100)

    def _validate_inputs(self) -> None:
        start = datetime.strptime(self.config.start_date, "%Y-%m-%d")
        end = datetime.strptime(self.config.end_date, "%Y-%m-%d")
        if start >= end:
            raise ValueError("开始日期必须早于结束日期")
        if self.config.initial_mode not in {"cash", "shares"}:
            raise ValueError("初始模式必须为 cash 或 shares")
        if self.config.initial_mode == "cash":
            if self.config.initial_capital <= 0:
                raise ValueError("初始资金必须大于 0")
            if self.config.initial_position != 0:
                raise ValueError("现金模式下初始持仓必须为 0")
        if self.config.initial_mode == "shares":
            if self.config.initial_position <= 0:
                raise ValueError("股份模式下初始持仓必须大于 0")
            if self.config.initial_capital != 0:
                raise ValueError("股份模式下初始资金必须为 0")
        if not isinstance(self.config.ticker, str) or len(self.config.ticker) != 6:
            raise ValueError("无效的股票代码格式（应为 6 位字符串）")
        if self.agent is None:
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

        cache = AkshareSQLiteCache(CACHE_PATH)
        cache_key = f"backtest_decision|{self.config.ticker}|{current_date}"
        if not self.config.force_run:
            cached_rows = cache.fetch_records(
                table="backtest_decision_cache",
                filters={"cache_key": cache_key},
                limit=1,
            )
            if cached_rows:
                cached_val = cached_rows[0].get("result")
                if cached_val:
                    try:
                        return json.loads(cached_val)
                    except Exception:
                        pass

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
                    return_state=True,
                )
                payload = {"decision": {"action": "hold", "quantity": 0}, "analyst_signals": {}}
                if isinstance(result, dict):
                    meta = result.get("metadata", {}) if isinstance(result, dict) else {}
                    decision_details = meta.get("portfolio_management_agent_decision_details")
                    if isinstance(decision_details, dict) and decision_details.get("action"):
                        payload["decision"] = {
                            "action": decision_details.get("action"),
                            "quantity": decision_details.get("quantity", 0),
                        }
                    # fallback: parse last message JSON if present
                    try:
                        last_msg = result.get("messages", [])[-1]
                        last_content = getattr(last_msg, "content", None)
                        if last_content:
                            parsed = json.loads(str(last_content))
                            if isinstance(parsed, dict) and parsed.get("action"):
                                payload["decision"] = {
                                    "action": parsed.get("action"),
                                    "quantity": parsed.get("quantity", 0),
                                }
                    except Exception:
                        pass
                else:
                    payload = {"decision": self._parse_decision_from_text(str(result)), "analyst_signals": {}}

                if not self.config.force_run:
                    cache.upsert_records(
                        table="backtest_decision_cache",
                        records=[
                            {
                                "cache_key": cache_key,
                                "result": json.dumps(payload, ensure_ascii=False),
                            }
                        ],
                        key_columns=["cache_key"],
                    )
                return payload
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
            decision["quantity"] = 0
        elif "sell" in text or "bearish" in text:
            decision["action"] = "sell"
            decision["quantity"] = 0
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

        self.logger.info("开始回测: %s (workflow)", self.config.ticker)
        llm_decision_log: list[Dict[str, Any]] = []
        print(
            f"{'日期':<12} {'代码':<6} {'操作':<6} {'数量':>8} {'价格':>10} "
            f"{'成交额':>12} {'现金变动':>12} {'现金':>12} {'持仓':>8} {'总值':>14} {'日收益%':>10}"
        )
        print("-" * 120)

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

        # 用前一交易日收盘价进行初始建仓（如果配置了初始持仓）
        if self.config.initial_position > 0 and not self._initial_position_applied:
            first_trade_date = pd.to_datetime(dates[0])
            prev_rows = price_df.loc[price_df["date"] < first_trade_date]
            if not prev_rows.empty:
                prev_row = prev_rows.iloc[-1]
                prev_price = float(prev_row.get("close", 0.0) or 0.0)
                prev_date = prev_row.get("date")
            else:
                prev_price = 0.0
                prev_date = None

            if prev_price > 0:
                if self.config.initial_mode == "shares":
                    target_qty = self.config.initial_position
                    self.portfolio["stock"] = target_qty
                    self._initial_position_applied = True
                    self.backtest_logger.info(
                        "初始建仓(持仓模式): %s 股 @ %.2f (前一交易日: %s)",
                        target_qty,
                        prev_price,
                        prev_date.strftime("%Y-%m-%d") if prev_date is not None else "未知",
                    )
                    print(
                        f"初始建仓(持仓模式): {target_qty} 股 @ {prev_price:.2f} (前一交易日: "
                        f"{prev_date.strftime('%Y-%m-%d') if prev_date is not None else '未知'})"
                    )
                else:
                    max_affordable = int(self.portfolio["cash"] // prev_price)
                    target_qty = min(self.config.initial_position, max_affordable)
                    if target_qty > 0:
                        self.portfolio["stock"] = target_qty
                        self.portfolio["cash"] -= target_qty * prev_price
                        self._initial_position_applied = True
                        self.backtest_logger.info(
                            "初始建仓: %s 股 @ %.2f (前一交易日: %s)",
                            target_qty,
                            prev_price,
                            prev_date.strftime("%Y-%m-%d") if prev_date is not None else "未知",
                        )
                        print(
                            f"初始建仓: {target_qty} 股 @ {prev_price:.2f} (前一交易日: "
                            f"{prev_date.strftime('%Y-%m-%d') if prev_date is not None else '未知'})"
                        )
            else:
                self.backtest_logger.warning("初始建仓失败: 未找到前一交易日收盘价")
                print("初始建仓失败: 未找到前一交易日收盘价")

        for idx, current_date in enumerate(dates):
            current_date_str = current_date.strftime("%Y-%m-%d")
            day_rows = price_df.loc[price_df["date"] == pd.to_datetime(current_date_str)]
            if day_rows.empty:
                continue
            current_price = float(day_rows.iloc[-1].get("open", 0.0) or 0.0)

            action = "hold"
            quantity = 0
            output: Dict[str, Any] = {}

            if idx % max(1, int(self.config.decision_interval)) == 0:
                lookback_start = (current_date - timedelta(days=30)).strftime("%Y-%m-%d")
                output = self._get_agent_decision(current_date_str, lookback_start)
                last_decision = output.get("decision", last_decision) or last_decision
                llm_decision_log.append(
                    {
                        "date": current_date_str,
                        "ticker": self.config.ticker,
                        "decision": dict(last_decision),
                        "response": output,
                    }
                )
            action = str(last_decision.get("action", "hold") or "hold")
            quantity = int(last_decision.get("quantity", 0) or 0)

            self.backtest_logger.info(f"\n交易日期: {current_date_str}")
            if output.get("analyst_signals"):
                self.backtest_logger.info("\n各智能体分析结果:")
                for agent_name, signal in output["analyst_signals"].items():
                    self.backtest_logger.info(f"\n{agent_name}: {signal}")
            requested_qty = quantity
            self.backtest_logger.info(f"行动: {action.upper()}")
            self.backtest_logger.info(f"请求数量: {requested_qty}")
            self.backtest_logger.info(
                "持仓: %s 股, 现金: %.2f",
                int(self.portfolio["stock"]),
                float(self.portfolio["cash"]),
            )

            executed_qty = self._execute_trade(action, requested_qty, current_price)
            trade_amount = executed_qty * current_price
            cash_change = 0.0
            if action == "buy":
                cash_change = -trade_amount
            elif action == "sell":
                cash_change = trade_amount

            self.backtest_logger.info("实际成交数量: %s", executed_qty)
            total_value = float(self.portfolio["cash"]) + float(self.portfolio["stock"]) * current_price
            self.portfolio["portfolio_value"] = total_value

            if self.portfolio_values:
                daily_return = (total_value / self.portfolio_values[-1]["Portfolio Value"] - 1) * 100
            else:
                daily_return = 0.0

            self.backtest_logger.info(
                "交易明细: action=%s qty=%s price=%.2f amount=%.2f cash_change=%.2f cash=%.2f stock=%s",
                action,
                executed_qty,
                current_price,
                trade_amount,
                cash_change,
                float(self.portfolio["cash"]),
                int(self.portfolio["stock"]),
            )

            print(
                f"{current_date_str:<12} {self.config.ticker:<6} {action:<6} {executed_qty:>8} "
                f"{current_price:>10.2f} {trade_amount:>12.2f} {cash_change:>12.2f} "
                f"{self.portfolio['cash']:>12.2f} {int(self.portfolio['stock']):>8} "
                f"{total_value:>14.2f} {daily_return:>10.2f}"
            )

            self.portfolio_values.append(
                {"Date": current_date, "Portfolio Value": total_value, "Daily Return": daily_return}
            )

        if llm_decision_log:
            try:
                with open(self._decision_log_path, "w", encoding="utf-8") as f:
                    json.dump(llm_decision_log, f, ensure_ascii=False, indent=2)
            except Exception as exc:  # noqa: BLE001
                self.backtest_logger.warning("写入 LLM 决策 JSON 失败: %s", exc)

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
        self.backtest_logger.info(f"最终持仓: {int(self.portfolio.get('stock', 0))} 股")
        self.backtest_logger.info(f"期末现金: {float(self.portfolio.get('cash', 0.0)):.2f}")

        # 可选绘图：默认不 show（避免无 GUI 环境卡死）
        # 默认保存图表到回测日志目录；仅在 plot=True 时弹窗展示
        if True:
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

                save_path = self.config.save_plot_path or os.path.join(
                    self._backtest_run_dir, "backtest_plot.png"
                )
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                plt.savefig(save_path, dpi=150)
                self.logger.info("Saved backtest plot to %s", save_path)
                if self.config.plot:
                    plt.show()
                plt.close(fig)
            except Exception as plot_err:  # noqa: BLE001
                self.logger.warning("Plot skipped due to error: %s", plot_err)

        return performance_df


def _parse_args() -> BacktestConfig:
    import argparse

    def _load_backtest_config_json() -> Dict[str, Any]:
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                return json.load(handle) or {}
        except Exception:
            return {}

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
    parser.add_argument("--initial-position", type=int, default=0, help="初始持仓股数（默认 0）")
    parser.add_argument("--num-of-news", type=int, default=5, help="每次调用工作流使用的新闻数量 (默认: 5)")
    parser.add_argument(
        "--decision-interval",
        type=int,
        default=1,
        help="每 N 个交易日运行一次工作流（默认 1）",
    )
    parser.add_argument("--plot", action="store_true", help="展示图表（可能阻塞），默认不展示")
    parser.add_argument("--save-plot", type=str, default="", help="保存图表到路径（默认保存到回测日志目录）")

    args = parser.parse_args()
    save_plot_path = args.save_plot or None
    if save_plot_path and not os.path.isabs(save_plot_path):
        save_plot_path = os.path.join(os.getcwd(), save_plot_path)

    config_payload = _load_backtest_config_json()
    backtest_cfg = config_payload.get("backtest", {}) if isinstance(config_payload, dict) else {}
    initial_mode = str(backtest_cfg.get("initial_mode", "")).strip().lower()
    if initial_mode not in {"cash", "shares"}:
        initial_mode = "cash"
    force_run = bool(backtest_cfg.get("force_run", False))

    if initial_mode == "shares":
        initial_position = int(backtest_cfg.get("initial_shares", 0) or 0)
        initial_capital = 0.0
    else:
        initial_position = 0
        initial_capital = float(backtest_cfg.get("initial_cash", args.initial_capital) or 0.0)

    return BacktestConfig(
        ticker=args.ticker,
        start_date=args.start_date,
        end_date=args.end_date,
        initial_mode=initial_mode,
        initial_capital=float(initial_capital),
        initial_position=int(initial_position or 0),
        force_run=force_run,
        num_of_news=args.num_of_news,
        decision_interval=args.decision_interval,
        plot=bool(args.plot),
        save_plot_path=save_plot_path,
    )


if __name__ == "__main__":
    cfg = _parse_args()
    from src.main import run_hedge_fund

    backtester = Backtester(cfg, agent=run_hedge_fund)
    backtester.run_backtest()
    backtester.analyze_performance()
