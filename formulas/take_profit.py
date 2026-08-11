"""
formulas/take_profit.py — 止盈线（纯函数，无副作用）

最终方案：止盈线随时间递增，每 24h 一个档位：
  第1档 1% → 第2档 2% → 第3档 3% ...
当账户总权益(eq) 达到当前档位对应百分比，且在该24h档位窗口内 → 全平止盈。
超过当前档位未达标，则继续网格运行，进入下一档。

注意：止盈判断用【账户总权益 eq】相对【本金基准 capital】的盈利百分比，
      capital = 建仓时总权益（含预留）。故预留的锁定资金不算盈利——须真赚够 eq 的1%才全平。
      风控/防堆仓则用【净浮盈 upl】，两者用途不同。
"""
from __future__ import annotations
import time


def current_tp_level(build_ts: float, base_pct: float = 1.0,
                     window_hours: float = 24.0, now: float | None = None) -> float:
    """当前止盈档位百分比。

    从建仓时刻 build_ts 起，每 window_hours 递增 base_pct。
    返回当前应达到的止盈线百分比（eq 盈利 ≥ 此百分比则触发全平）。
    """
    if build_ts <= 0:
        return base_pct
    now = now or time.time()
    elapsed_h = (now - build_ts) / 3600.0
    level = int(elapsed_h // window_hours) + 1  # 第1档=1个window内
    return base_pct * level


def should_take_profit(build_ts: float, total_equity: float, capital: float,
                       base_pct: float = 1.0, window_hours: float = 24.0,
                       now: float | None = None) -> tuple[bool, float]:
    """判断是否触发止盈全平。

    eq 相对 capital 的盈利达到当前档位百分比 → 触发。
    返回 (是否触发, 当前档位百分比)。
    """
    if capital <= 0 or total_equity is None:
        return False, 0.0
    tp = current_tp_level(build_ts, base_pct, window_hours, now)
    profit_ratio = (total_equity - capital) / capital * 100
    # 浮点容差：消除 (eq-capital)/capital*100 在整档边界(如1.0%)因精度差一点导致漏触发
    return profit_ratio + 1e-9 >= tp, tp
