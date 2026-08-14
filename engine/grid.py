"""engine/grid.py — 网格触发与挂单（决策层）

最终方案（统一联动架构定稿）：
  - 锚点 = 交易所最新成交价 last_px（解决成交均价滞涨），无则回退 mark_px
  - 间距 = 真实波幅 ATR（绝对价，日线TR均值），不放大DR、不用24h振幅权重
  - 挂单价 = 锚点 × (1 ± 间距%)，再被交易所单侧 bePx 双夹逼
      上端(平多+开空) = max(锚×(1+间距), 多头bePx)   # 平多别亏
      下端(平空+开多) = min(锚×(1−间距), 空头bePx)   # 平空别亏
  - 4方向挂单：上端组(平多N+开空N) + 下端组(平空N+开多N)
  - 事件驱动：查 pending 挂单，ordId 消失=成交 → 撤对侧+重挂（锚自动=新成交价）
"""
from __future__ import annotations
import logging
import time

from state import get_state, save_state
from diagnostic_logger import get_diag_logger
from formulas.grid import grid_spacing_pct
from formulas.safety import calc_imbalance_rate
from formulas.price_precision import px_round  # 价格按交易所 tickSz 对齐（精度地基，保证盈亏真实）

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="GRID", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="网格")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


# OKX clOrdId 归属前缀：引擎挂单都带此前缀，用于启动/恢复时区分"我的单 / 幽灵单"。
# 不同部署必须用不同前缀，避免跨实例误认领。
_CLORDID_PREFIX = "gw2seo"


def _gen_clordid(idx: int) -> str:
    """生成 OKX clOrdId（客户端自定义订单ID，当前挂单唯一，≤32位）。

    前缀=引擎标识防碰撞；时间戳+单序号保证同批每单唯一（OKX 要求 live 单唯一）。
    """
    return f"{_CLORDID_PREFIX}{int(time.time() * 1000) % 100000000000}{idx}"


def adopt_or_reset_grid_orders(st, client=None, pending=None) -> bool:
    """按 clOrdId 前缀认领交易所现有挂单（自愈式无损接管）。

    - 带 _CLORDID_PREFIX 标记的单 → 按 side 分组重建 grid_upper/lower_ord_ids
      （sell=上端平多+开空，buy=下端平空+开多），save_state，返回 True。
    - 无标记的单（幽灵/手动/别的实例）→ 不收养，报警提示，不撤不挂。
    - API 异常 → 记日志返回 False，不破坏现有状态（下轮重试）。
    """
    from data.exchange import get_auth_client
    if client is None:
        client = get_auth_client()
    if client is None:
        return False
    try:
        if pending is None:
            pending = client.get_orders_pending(st.inst_id) or []
    except Exception as e:
        logger.warning(f"认领前查 pending 异常: {e}")
        return False
    own = [o for o in pending
           if str(o.get("clOrdId", "")).startswith(_CLORDID_PREFIX)]
    others = [o for o in pending
              if not str(o.get("clOrdId", "")).startswith(_CLORDID_PREFIX)]
    if others:
        _log(st, f"⚠️ 发现 {len(others)} 笔非本引擎挂单(无 {_CLORDID_PREFIX} 标记)，"
                 f"不收养: {[o.get('ordId') for o in others]}", level="WARN", cat="GRID")
    if not own:
        return False
    st.grid_upper_ord_ids = [o["ordId"] for o in own if o.get("side") == "sell"]
    st.grid_lower_ord_ids = [o["ordId"] for o in own if o.get("side") == "buy"]
    save_state()
    _log(st, f"🔁 认领现有挂单: 上={st.grid_upper_ord_ids} 下={st.grid_lower_ord_ids}",
         cat="GRID")
    return True


def _grid_step_contracts(st) -> int:
    """网格每步换仓张数 N = max(int(总张数 × adj_ratio ÷ 2), 1)。

    adj_ratio 是前端可调比例参数（默认0.06），每步换仓占 half 总仓的 adj_ratio。
    这是公式驱动的换仓量，不是硬编码固定值。
    """
    pos = st.position
    total = pos.long_contracts + pos.short_contracts
    ratio = getattr(st, "adj_ratio", 0.06) or 0.06
    return max(int(total * ratio / 2), 1)


def _notify_trade(st, desc, side):
    """成交后推送(平多/开空 或 平空/开多)。只读, 异常不影响交易。"""
    try:
        # 先重拉交易所余额, 推送成交后的真实权益(成交当轮 total_equity 还是旧值)
        try:
            from data.exchange import sync_equity
            sync_equity()
        except Exception:
            pass
        from notify import on_trade
        pos = st.position
        px = getattr(st, f"grid_{side}_px", 0) or 0
        n = _grid_step_contracts(st)
        on_trade(desc, n, px_round(st.inst_id, px), round(st.total_equity or 0, 2),
                 pos.long_contracts, pos.short_contracts)
    except Exception as e:
        logger.debug(f"notify_trade: {e}")


def _fetch_all_order_history(client, inst_id):
    """拉全订单历史(orders-history + archive，分页，before 拉更早)。"""
    orders = []
    for ep in ("/api/v5/trade/orders-history", "/api/v5/trade/orders-history-archive"):
        before_ts = ""
        guard = 0
        while guard < 30:
            guard += 1
            p = {"instType": "SWAP", "instId": inst_id, "limit": "100"}
            if before_ts:
                p["before"] = before_ts
            try:
                r = client._request("GET", ep, params=p)
            except Exception as e:
                logger.warning(f"拉订单历史异常({ep}): {e}")
                break
            items = r.get("data", []) if isinstance(r, dict) else r
            if not items:
                break
            orders.extend(items)
            before_ts = items[-1]["cTime"]
    return orders


def backfill_grid_stats(st) -> None:
    """回填网格滚动次数与累加已实现收益（系统自动识别本次网格起点，不靠人告诉）。

    逻辑：订单历史倒序追踪持仓 → 找最近一次"持仓归零"（旧轮次结束）
          → 归零后第一次成对建仓 = 本次网格起点；之前旧轮次自动排除。
    滚动次数 = 起点后成对平仓成交次数；收益 = 起点后账单 pnl 累加(覆盖 fills 窗口外)。
    幂等：仅当 grid_count==0 时执行；失败不破坏现有状态。
    """
    if (getattr(st, "grid_count", 0) or 0) > 0:
        return
    try:
        from data.exchange import get_auth_client
        client = get_auth_client()
        if client is None:
            return
        inst = st.inst_id
        orders = _fetch_all_order_history(client, inst)
        if not orders:
            return
        # ① 倒序追踪持仓，找最近一次"持仓归零"时刻(旧轮次结束)
        # 当前持仓用交易所真实值(引擎内存 st.position 可能不同步 → 会找偏归零点)
        cur_long, cur_short = 0, 0
        try:
            pos = client._request("GET", "/api/v5/account/positions",
                                  params={"instType": "SWAP", "instId": inst})
            for _p in (pos.get("data", []) if isinstance(pos, dict) else pos):
                if _p.get("posSide") == "long":
                    cur_long = int(float(_p.get("pos") or 0))
                elif _p.get("posSide") == "short":
                    cur_short = int(float(_p.get("pos") or 0))
        except Exception:
            cur_long = int(getattr(st.position, "long_contracts", 0) or 0)
            cur_short = int(getattr(st.position, "short_contracts", 0) or 0)
        orders_desc = sorted(orders, key=lambda o: o.get("cTime", "0"), reverse=True)
        start_ts = None
        for o in orders_desc:
            if o.get("state") != "filled":
                continue
            side = o.get("side"); pos = o.get("posSide"); sz = int(o.get("sz") or 0)
            if pos == "long":
                cur_long += sz if side == "sell" else -sz
            elif pos == "short":
                cur_short += sz if side == "buy" else -sz
            if cur_long <= 0 and cur_short <= 0:
                start_ts = o.get("cTime")
                break
        if start_ts is None:
            start_ts = orders_desc[-1].get("cTime", "0")
        # ② 拉全账单(带重试避开限流) — 滚动/已实现/手续费统一从账单(权威)
        bills = []
        try:
            before_ts = ""
            guard = 0
            while guard < 50:
                guard += 1
                p = {"instType": "SWAP", "instId": inst, "limit": "100"}
                if before_ts:
                    p["before"] = before_ts
                ok = False
                for _a in range(5):
                    try:
                        r = client._request("GET", "/api/v5/account/bills", params=p)
                        ok = True
                        break
                    except Exception:
                        time.sleep(2)
                if not ok:
                    break
                items = r.get("data", []) if isinstance(r, dict) else r
                if not items:
                    break
                bills.extend(items)
                before_ts = items[-1]["ts"]
        except Exception as e:
            logger.warning(f"回填拉账单失败: {e}")
        # ③ 滚动次数 + 已实现 + 手续费: 账单 subType5/6=平多/平空(权威)
        roll_cnt = 0
        pnl_total = 0.0
        fee_total = 0.0
        seen = set()
        for b in bills:
            ts = b.get("ts", "")
            if not ts or int(ts) <= int(start_ts):
                continue
            st_ = int(b.get("subType") or 0)
            if st_ in (5, 6):
                k = (b.get("ts"), b.get("ordId"), b.get("sz"))
                if k not in seen:
                    seen.add(k)
                    roll_cnt += 1
                pnl_total += float(b.get("pnl") or 0)
            if st_ in (1, 2, 3, 4, 5, 6):
                fee_total += float(b.get("fee") or 0)  # fee 为负(支出)
        st.grid_count = roll_cnt
        st.total_pnl = round(pnl_total, 2)
        st.total_fee = round(abs(fee_total), 2)  # 正数: 前端 已实现+浮盈-手续费
        save_state()
        _log(st, f"📊 回填网格统计: 滚动{roll_cnt}次, 已实现{pnl_total:+.2f}U, 手续费{abs(fee_total):.2f}U", cat="GRID")
    except Exception as e:
        logger.warning(f"回填网格统计失败: {e}")


def _mode_base_density(st) -> float:
    """模式base密度：进攻=base_density，防守=defense_density（文档§5.1）。"""
    if getattr(st, "mode", "attack") == "defense":
        return float(getattr(st, "defense_density", 2.0) or 2.0)
    return float(getattr(st, "base_density", 2.0) or 2.0)


def _ladder_density(st, imbalance) -> float:
    """档位表密度：取失衡率对应档（≥该档取该档密度），未达档位取 base。文档§2.3。

    2026-08-13：新增档位勾选（ladder_enabled）——未勾选的档跳过不参与判定。
    遍历时只考虑"启用且失衡率达到"的档，取最高的那一档密度。
    """
    rates = list(getattr(st, "ladder_rates", [40, 50, 60, 70, 80]) or [40, 50, 60, 70, 80])
    dens = list(getattr(st, "ladder_densities", [1.4, 1.0, 0.75, 0.5, 0.3]) or [1.4, 1.0, 0.75, 0.5, 0.3])
    enabled = list(getattr(st, "ladder_enabled", [True, True, True, True, True]) or [True, True, True, True, True])
    d = float(getattr(st, "base_density", 2.0) or 2.0)
    for i, r in enumerate(rates):
        if i >= len(enabled) or not enabled[i]:
            continue  # 未勾选档：跳过
        dd = dens[i] if i < len(dens) else None
        if imbalance >= r and dd is not None:
            d = float(dd)
    return d


def _heavy_side(st):
    """重仓侧：多/空张数大的那侧，返回 ('long'|'short', 张数)。平手返回 None。"""
    pos = st.position
    if pos.long_contracts > pos.short_contracts:
        return "long", pos.long_contracts
    if pos.short_contracts > pos.long_contracts:
        return "short", pos.short_contracts
    return None, 0


def _heavy_upl_pct(st, heavy: str) -> float:
    """重仓侧 upl%（分母=账户权益−累计追加本金，统一口径）。文档§2.7.2。"""
    pos = st.position
    denom = max(float(st.total_equity or 0) - float(getattr(st, "cumulative_added", 0.0) or 0), 1e-9)
    if heavy == "long":
        return pos.long_unrealized_pnl / denom * 100
    return pos.short_unrealized_pnl / denom * 100


def _debounce_upl_state(st, heavy: str, upl_pct: float) -> str:
    """防抖状态机：返回 'profit'|'loss'。死区保持上一状态，初始=在赚。文档§2.7.5。"""
    now = time.time()
    loss_line = float(getattr(st, "debounce_loss_line", -0.5) or -0.5)
    profit_line = float(getattr(st, "debounce_profit_line", 0.2) or 0.2)
    confirm = float(getattr(st, "confirm_time", 30.0) or 30.0)

    prev = getattr(st, "_debounce_state", "profit")
    ts = getattr(st, "_debounce_ts", 0.0)
    prev_heavy = getattr(st, "_debounce_heavy", None)

    # 重仓侧变化 → 重置计时（新方向从零开始判定）
    if heavy != prev_heavy:
        st._debounce_heavy = heavy
        ts = 0.0

    # 当前候选状态
    if upl_pct < loss_line:
        cand = "loss"
    elif upl_pct > profit_line:
        cand = "profit"
    else:
        cand = prev  # 死区：保持上一状态

    if cand == prev:
        pass  # 状态稳定，仅刷新时间
    else:
        # 状态变化需要持续确认时间
        if ts == 0.0:
            ts = now
        elif now - ts >= confirm:
            prev = cand
            ts = now
    st._debounce_ts = ts
    st._debounce_state = prev
    return prev


def _is_emergency(st, imbalance: float, heavy_state: str) -> bool:
    """紧急 = 失衡≥单向阈值 AND 重仓在亏(防抖后)。退出带缓冲(exit_buffer)。文档§2.7.4/2.7.5。"""
    threshold = float(getattr(st, "one_way_threshold", 60.0) or 60.0)
    buffer = float(getattr(st, "exit_buffer", 5.0) or 5.0)
    was_emergency = getattr(st, "_emergency", False)
    if was_emergency:
        # 退出两条路径：失衡回落<阈值−缓冲，或 重仓转赚
        if imbalance < (threshold - buffer) or heavy_state == "profit":
            st._emergency = False
            return False
        return True
    # 进入：失衡≥阈值 且 在亏(防抖后)
    if imbalance >= threshold and heavy_state == "loss":
        st._emergency = True
        return True
    return False


def _is_one_way(st, imbalance: float) -> bool:
    """单向成交 = 失衡率 ≥ 单向成交阈值（与盈亏无关）。文档§2.4/裁决链§三。"""
    threshold = float(getattr(st, "one_way_threshold", 60.0) or 60.0)
    return imbalance >= threshold


def calc_grid_levels(st) -> tuple[float, float]:
    """计算挂单上下价（成交价锚 + ATR间距×密度 + bePx条件钳制）。返回 (upper, lower)。

    分状态（文档§2.7.3 四状态权威表）：
      - 紧急(高失衡+在亏防抖后) → 密度=min(模式base,档位)，拆墙贴锚点(不夹逼)
      - 正常(含高失衡+在赚)     → 密度=模式base，bePx夹逼(max/min)
    """
    pos = st.position
    # 锚点 = 固定锚(grid_anchor_px，建仓=中轴/成交=成交价时设定)，绝不随市价漂。
    # 无固定锚(旧仓未设/首次)才回退多空开仓均价中轴(成本锚)，绝不用 last_px/mark_px 市价。
    if st.grid_anchor_px > 0:
        anchor = st.grid_anchor_px
    else:
        la = pos.long_avg_px or 0.0
        sa = pos.short_avg_px or 0.0
        anchor = (la + sa) / 2 if (la > 0 and sa > 0) else (pos.last_px or pos.mark_px)

    imbalance = calc_imbalance_rate(pos.long_contracts, pos.short_contracts)
    heavy, _ = _heavy_side(st)
    # 无持仓或完全对冲 → 不判定状态，用模式base密度、bePx夹逼
    # 档位密度实时生效（2026-08-14 用户定案）：失衡率 ≥ 任一启用档位阈值 → 立即用该档密度缩窄。
    # _ladder_density 内部：未达档位取 base_density，达到档位取该档密度（勾选才参与）。
    # 回补后失衡率回落到某档 → 自动落到该档密度（如回补到40% → 落回档1密度）。
    # 紧急(单边防御)与正常共用同一套档位密度；紧急只影响"单向成交模式"判定，不影响密度选择。
    if heavy is None:
        density = _mode_base_density(st)
        emergency = False
        heavy_state = "profit"
    else:
        upl_pct = _heavy_upl_pct(st, heavy)
        heavy_state = _debounce_upl_state(st, heavy, upl_pct)
        emergency = _is_emergency(st, imbalance, heavy_state)
        was_em = getattr(st, "emergency_state", False)
        if emergency and not was_em:
            # 失衡率过高 → 启动单边防御(只挂平重仓侧)，推送
            try:
                from notify import on_imbalance
                on_imbalance(round(imbalance, 1),
                             float(getattr(st, "one_way_threshold", 60.0) or 60.0), heavy)
            except Exception as e:
                logger.debug(f"notify_imbalance: {e}")
        density = _ladder_density(st, imbalance)

    # 间距 = ATR% × 当前网格密度。ATR未就绪时回退到可调间距参数。
    atr = getattr(st, "atr_abs", 0.0) or 0.0
    if atr > 0 and anchor > 0:
        spacing = grid_spacing_pct(atr, anchor, density)
    else:
        spacing = getattr(st, "target_spacing_pct", 0.6) or 0.6
    if spacing <= 0:
        spacing = 0.6
    # 网格密度下限钳制（文档§1.1）：密度 ≥ max(滑块设定, 2×费率÷ATR%)
    # 让间距价格覆盖手续费（单位自洽）。ATR% = atr/anchor*100。
    if atr > 0 and anchor > 0:
        atr_pct = atr / anchor * 100  # ATR% 已是百分比数（如0.5 = 0.5%）
        if atr_pct > 0:
            fee_pct = 0.001  # 单边费率占位（0.1%）
            density_floor = max(float(getattr(st, "density_min", 0.3) or 0.3), 2 * fee_pct / atr_pct)
            density = max(density, density_floor)
            # 密度可能被钳制抬升 → 重算间距
            spacing = grid_spacing_pct(atr, anchor, density)

    upper = anchor * (1 + spacing / 100)
    lower = anchor * (1 - spacing / 100)

    # bePx 双夹逼（仅 normal/在赚；紧急拆墙贴锚点不夹逼）
    if not emergency:
        if pos.long_be_px > 0:
            upper = max(upper, pos.long_be_px)
        if pos.short_be_px > 0:
            lower = min(lower, pos.short_be_px)

    # 记录展示（价格按交易所 tickSz 对齐，保证锁定收益等计算用真实精度）
    st.grid_spacing_pct = round(spacing, 2)
    st.grid_upper_px = px_round(st.inst_id, upper)
    st.grid_lower_px = px_round(st.inst_id, lower)
    st.current_density = round(density, 3)
    # 注意：不写 st.grid_anchor_px —— 锚点只允许在 建仓/成交 时更新，防被挂单循环污染成市价。
    st.emergency_state = emergency
    st.heavy_side = heavy
    st.heavy_state = heavy_state
    return upper, lower


def _cancel_orders(st, ord_ids: list) -> None:
    """按 ordId 精确撤销剩余网格单（不误伤其它单）。

    撤单安全保险：撤单前逐单查交易所真实状态，只撤「未成交(live)」的挂单；
    已成交(filled)已进仓位、已撤(canceled)、查询失败/无数据 → 全部跳过，绝不撤。
    交易所语义：当前委托（未成交挂单）可撤；当前仓位（已成交）不可撤、也不该撤。
    """
    if not ord_ids:
        return
    from data.exchange import get_auth_client
    client = get_auth_client()
    if client is None:
        return
    for oid in ord_ids:
        try:
            o = client.get_order(st.inst_id, oid)
        except Exception as e:
            logger.debug(f"撤单前查状态 {oid} 异常: {e} → 跳过，不撤")
            continue
        if not isinstance(o, dict):
            logger.debug(f"撤单跳过 {oid}（查询无数据）")
            continue
        state = o.get("state", "")
        # 身份校验（自愈认领冲突解决）：只撤带本引擎 clOrdId 标记的单，
        # 无标记（另一实例/手动/幽灵单）绝不撤，避免误撤别人挂的单
        cloid = str(o.get("clOrdId", ""))
        if not cloid.startswith(_CLORDID_PREFIX):
            logger.debug(f"撤单跳过 {oid}（clOrdId={cloid!r} 非本引擎，不撤）")
            continue
        # 只撤未成交(live)的挂单；已成交/已撤/状态未知一律跳过
        if state and state != "live":
            logger.debug(f"撤单跳过 {oid}（state={state}，非未成交，绝不撤）")
            continue
        try:
            client.cancel_order(st.inst_id, oid)
        except Exception as e:
            logger.debug(f"撤单 {oid} 异常: {e}")


def _verify_filled(client, inst_id: str, ord_ids: list) -> tuple[bool, dict, float, float]:
    """逐个核实 ordId 的真实状态，判断是否真正成交。

    背景：grid.py 原判定把"pending 查询结果中缺单/空"直接当"成交"，网络抖动时
    pending 查询可能返回空 → 误判成交 → 误撤。这里改为逐单查交易所真实 state：
      - filled            → 真成交
      - canceled          → 被撤（非成交，诊断）
      - live/空           → 仍在挂单中（pending查询延迟/异常）→ 不算成交
      - 查询抛异常/无数据 → 无法确认 → 不算成交，由调用方跳过本轮
    返回 (是否确认真成交, 各单state明细, 成交订单累计手续费)。任何一单查询失败返回 (False, {}, 0)。
    """
    states: dict[str, str] = {}
    fees = 0.0
    for oid in ord_ids:
        try:
            o = client.get_order(inst_id, oid)
        except Exception as e:
            logger.warning(f"核实成交状态失败 {oid}: {e} → 本轮跳过，不判定成交")
            return False, {}, 0, 0.0
        stt = (o or {}).get("state", "") if isinstance(o, dict) else ""
        if not stt:
            logger.warning(f"核实成交状态无数据 {oid} → 本轮跳过，不判定成交")
            return False, {}, 0, 0.0
        states[oid] = stt
        try:
            fees += abs(float((o or {}).get("fee") or 0))
        except Exception:
            pass
    filled = all(states.get(oid) == "filled" for oid in ord_ids) if ord_ids else False
    # 汇总成交均价（用最后一笔 filled 单的 avgPx，作为成交后锚点）
    fill_px = 0.0
    if filled:
        for oid in ord_ids:
            if states.get(oid) == "filled":
                try:
                    _o = client.get_order(inst_id, oid)
                    _px = float((_o or {}).get("avgPx") or 0)
                    if _px > 0:
                        fill_px = _px
                except Exception:
                    continue
    return filled, states, fees, fill_px


def place_grid_orders(st) -> dict:
    """挂网格单：上端组(平多+开空) + 下端组(平空+开多)，各 N 张限价单。

    成交模式（文档§2.4/裁决链§三）：
      - 双向（失衡<单向阈值）：上、下都挂
      - 单向（失衡≥单向阈值）：只挂"平重仓侧"单，撤另一单
    """
    from data.exchange import get_auth_client
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id

    # 撤掉旧网格单，避免重复挂
    old = list(st.grid_upper_ord_ids) + list(st.grid_lower_ord_ids)
    if old:
        _cancel_orders(st, old)
    st.grid_upper_ord_ids = []
    st.grid_lower_ord_ids = []
    upper_ids, lower_ids = [], []

    upper, lower = calc_grid_levels(st)
    # 记录挂单时的实际密度（档位密度实时生效用）：独立字段，不被 _refresh_grid_display 覆盖
    st.grid_placed_density = round(getattr(st, "current_density", 0.0) or 0.0, 3)
    n = _grid_step_contracts(st)

    # 成交模式：单向只挂平重仓侧
    pos = st.position
    imbalance = calc_imbalance_rate(pos.long_contracts, pos.short_contracts)
    one_way = _is_one_way(st, imbalance)
    heavy, _ = _heavy_side(st)

    # 基础4单：上端(平多+开空)@upper，下端(平空+开多)@lower
    # 每单带 clOrdId 归属标记（同批 idx 0-3 保证唯一）
    upper_orders = [
        {"instId": inst, "tdMode": "cross", "side": "sell", "posSide": "long",
         "ordType": "limit", "px": str(upper), "sz": str(n), "clOrdId": _gen_clordid(0)},
        {"instId": inst, "tdMode": "cross", "side": "sell", "posSide": "short",
         "ordType": "limit", "px": str(upper), "sz": str(n), "clOrdId": _gen_clordid(1)},
    ]
    lower_orders = [
        {"instId": inst, "tdMode": "cross", "side": "buy", "posSide": "short",
         "ordType": "limit", "px": str(lower), "sz": str(n), "clOrdId": _gen_clordid(2)},
        {"instId": inst, "tdMode": "cross", "side": "buy", "posSide": "long",
         "ordType": "limit", "px": str(lower), "sz": str(n), "clOrdId": _gen_clordid(3)},
    ]

    if one_way and heavy is not None:
        # 单向：只挂平重仓侧
        if heavy == "short":
            # 重仓空 → 只挂下端(开多平空，平空头)，撤上端(会开新空头)
            orders = list(lower_orders)
            upper_ids, lower_ids = [], []
            _log(st, f"➡️ 单向(重仓空): 只挂下端{lower:.1f}(开多平空)，撤上端",
                 data={"imbalance": round(imbalance, 1), "heavy": "short"})
        else:
            # 重仓多 → 只挂上端(平多开空，平多头)，撤下端(会开新多头)
            orders = list(upper_orders)
            upper_ids, lower_ids = [], []
            _log(st, f"➡️ 单向(重仓多): 只挂上端{upper:.1f}(平多开空)，撤下端",
                 data={"imbalance": round(imbalance, 1), "heavy": "long"})
    else:
        orders = upper_orders + lower_orders

    try:
        result = client.batch_orders(orders)
    except Exception as e:
        _log(st, f"❌ 挂单异常: {e}", level="ERROR")
        return {"status": "error", "msg": str(e)}

    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 挂单被拒: {result}", level="ERROR")
        return {"status": "error", "msg": str(result.get("msg", "") if result else "无响应")}

    # 回填 ordId：单向时只有一组（全在上端或全在下端），双向时前2=上端组、后2=下端组
    subs = result.get("data", [])
    if one_way and heavy is not None:
        for so in subs:
            oid = so.get("ordId", "")
            if heavy == "short":
                lower_ids.append(oid)
            else:
                upper_ids.append(oid)
    else:
        for i, so in enumerate(subs):
            oid = so.get("ordId", "")
            if i < 2:
                upper_ids.append(oid)
            else:
                lower_ids.append(oid)
    st.grid_upper_ord_ids = upper_ids
    st.grid_lower_ord_ids = lower_ids
    st.grid_placed_ts = time.time()   # 记录挂单时间（24h不成交自动重挂用）
    save_state()
    _log(st, f"📊 挂单: 上{upper:.1f}(平多+开空)/下{lower:.1f}(平空+开多) 各{n}张"
             f" (密度{getattr(st, 'current_density', 2.0)}, 单向={one_way})",
         data={"upper": upper, "lower": lower, "n": n,
               "upper_ids": upper_ids, "lower_ids": lower_ids,
               "density": getattr(st, "current_density", 2.0), "one_way": one_way})
    # 挂单成功后 → 企微推送挂单价/张数（只读, 异常不影响挂单）
    try:
        from notify import on_op
        mode = "单向(重仓" + ("多" if one_way and heavy == "long" else "空") + ")" if one_way and heavy is not None else "双向"
        on_op("📊 网格挂单", "\n".join([
            f"{inst}",
            f"上 {px_round(inst, upper)} (平多+开空) {n}张",
            f"下 {px_round(inst, lower)} (平空+开多) {n}张",
            f"密度{getattr(st, 'current_density', 2.0)} · {mode}",
        ]))
    except Exception:
        pass
    return {"status": "ok", "data": {"upper": upper, "lower": lower, "n": n}}


def check_grid_tick(st) -> None:
    """网格主流程：无单→挂单；单成交(ordId消失)→撤对侧+重挂。

    事件驱动：挂单组 ordId 不再出现在 pending 里 = 该方向成交（价格穿过了挂单价）。
    成交本身由限价单自动完成（平多开空/平空开多），这里只负责撤对侧 + 以新成交价重挂。
    """
    if not st.running or st.paused:
        return
    if not getattr(st, "grid_auto_run", True):
        return
    from data.exchange import get_auth_client
    client = get_auth_client()
    if client is None:
        return

    # 锚点自愈：若固定锚尚未设定(旧仓/历史持仓)，用多空开仓均价中轴补上(成本锚，绝不用市价)。
    # 一旦设了，就只允许在 建仓/成交 时更新，绝不随市价漂。
    if st.grid_anchor_px <= 0:
        _la = st.position.long_avg_px or 0.0
        _sa = st.position.short_avg_px or 0.0
        if _la > 0 and _sa > 0:
            st.grid_anchor_px = (_la + _sa) / 2
            save_state()

    try:
        pending = client.get_orders_pending(st.inst_id) or []
    except Exception as e:
        logger.debug(f"查 pending 异常: {e}")
        return
    pending_ids = {o.get("ordId", "") for o in pending}

    up = st.grid_upper_ord_ids or []
    lo = st.grid_lower_ord_ids or []

    # 没有有效挂单追踪 → 先尝试按 clOrdId 认领交易所现有挂单(自愈式无损接管)
    if not up and not lo:
        adopted = adopt_or_reset_grid_orders(st, client, pending)
        if adopted:
            return  # 已认领接管，下轮按"有挂单记录"正常管理
        # 无带标记单：若交易所存在无标记挂单(幽灵/手动/别的实例) → 不撤不挂，报警等用户
        has_ghost = any(
            not str(o.get("clOrdId", "")).startswith(_CLORDID_PREFIX)
            for o in pending)
        if has_ghost:
            return
        # 无任何挂单（干净）→ 正常新建
        place_grid_orders(st)
        return

    # 事件驱动：任一方向组"任一成员不在 pending" → 先核实交易所真实状态，
    # 确认真成交(filled)才触发撤对侧重挂；pending 查询为空/异常绝不当作成交。
    # 背景：网络抖动时 pending 查询可能返回空 → 原逻辑误判成交 → 误撤。
    up_missing = [oid for oid in up if oid not in pending_ids]
    lo_missing = [oid for oid in lo if oid not in pending_ids]

    # 上端可能成交：核实
    if up_missing:
        filled_up, states_up, fees_up, fill_up_px = _verify_filled(client, st.inst_id, up_missing)
        if not filled_up:
            # 无法确认成交（查询失败/仍在挂单/被撤）→ 跳过本轮，记诊断日志，不误撤
            logger.warning(
                f"上端单疑似变动但未确认真成交: missing={up_missing} "
                f"states={states_up} pending空={not pending_ids} → 跳过本轮"
            )
            # 若待核实单在交易所全部查无此单(states空) → 脏追踪残留
            # (历史切换/撤单遗留的ordId), 清掉重新挂, 否则引擎永久卡住
            if not states_up and up_missing and not pending_ids:
                logger.warning(f"上端挂单追踪为脏ID({up_missing}) 交易所无此单 → 清残留重新挂单")
                st.grid_upper_ord_ids = []
                st.grid_lower_ord_ids = []
                save_state()
                place_grid_orders(st)
            return
        _log(st, f"🔺 上端成交(平多+开空) → 撤下端, 重挂", cat="GRID")
        _notify_trade(st, "平多/开空", "upper")
        # 锚点 = 多头成交价（不随市价漂）；记录最近成交时间（12h提醒计时起点）
        if fill_up_px > 0:
            st.grid_anchor_px = fill_up_px
        st.grid_last_trade_ts = time.time()
        # 网格滚动次数+1；锁定上端已实现收益 = N×面值×(挂单价upper−多头开仓均价)
        st.grid_count = (st.grid_count or 0) + 1
        _pnl_up = (_grid_step_contracts(st) * float(st.ct_val or 0)
                   * (float(st.grid_upper_px or 0) - float(st.position.long_avg_px or 0)))
        st.total_pnl = round((st.total_pnl or 0) + _pnl_up, 2)
        st.total_fee = round((st.total_fee or 0) + fees_up, 2)
        _log(st, f"💰 上端成交锁定 {_pnl_up:+.2f}U (网格#{st.grid_count})", cat="GRID")
        if lo:
            _cancel_orders(st, [oid for oid in lo if oid in pending_ids])
        st.grid_upper_ord_ids = []
        st.grid_lower_ord_ids = []
        save_state()
        place_grid_orders(st)
        return

    # 下端可能成交：核实
    if lo_missing:
        filled_lo, states_lo, fees_lo, fill_lo_px = _verify_filled(client, st.inst_id, lo_missing)
        if not filled_lo:
            logger.warning(
                f"下端单疑似变动但未确认真成交: missing={lo_missing} "
                f"states={states_lo} pending空={not pending_ids} → 跳过本轮"
            )
            # 若待核实单在交易所全部查无此单(states空) → 脏追踪残留, 清掉重新挂
            if not states_lo and lo_missing and not pending_ids:
                logger.warning(f"下端挂单追踪为脏ID({lo_missing}) 交易所无此单 → 清残留重新挂单")
                st.grid_upper_ord_ids = []
                st.grid_lower_ord_ids = []
                save_state()
                place_grid_orders(st)
            return
        _log(st, f"🔻 下端成交(平空+开多) → 撤上端, 重挂", cat="GRID")
        _notify_trade(st, "平空/开多", "lower")
        # 锚点 = 空头成交价（不随市价漂）；记录最近成交时间（12h提醒计时起点）
        if fill_lo_px > 0:
            st.grid_anchor_px = fill_lo_px
        st.grid_last_trade_ts = time.time()
        # 网格滚动次数+1；锁定下端已实现收益 = N×面值×(空头开仓均价−挂单价lower)
        st.grid_count = (st.grid_count or 0) + 1
        _pnl_lo = (_grid_step_contracts(st) * float(st.ct_val or 0)
                   * (float(st.position.short_avg_px or 0) - float(st.grid_lower_px or 0)))
        st.total_pnl = round((st.total_pnl or 0) + _pnl_lo, 2)
        st.total_fee = round((st.total_fee or 0) + fees_lo, 2)
        _log(st, f"💰 下端成交锁定 {_pnl_lo:+.2f}U (网格#{st.grid_count})", cat="GRID")
        if up:
            _cancel_orders(st, [oid for oid in up if oid in pending_ids])
        st.grid_upper_ord_ids = []
        st.grid_lower_ord_ids = []
        save_state()
        place_grid_orders(st)
        return

    # 两侧都在挂单等待中：① 失衡率跨档实时重挂（档位密度实时生效）② 超时未成交自动重挂
    # 改动：档位密度实时生效（2026-08-14）——当前失衡率对应档位密度 ≠ 挂单时密度 → 撤单按新密度重挂
    try:
        from formulas.safety import calc_imbalance_rate
        _imb_now = calc_imbalance_rate(st.position.long_contracts, st.position.short_contracts)
        _dens_now = _ladder_density(st, _imb_now)
        # grid_placed_density 在挂单时由 place_grid_orders 记录（不被 _refresh_grid_display 覆盖）
        _dens_placed = getattr(st, "grid_placed_density", None)
        # 若跨档导致密度变化 → 实时重挂
        if _dens_placed is not None and abs(_dens_now - _dens_placed) > 1e-9:
            _log(st, f"⚙ 失衡跨档 密度 {_dens_placed}→{_dens_now} → 实时撤单重挂", cat="GRID")
            place_grid_orders(st)
            return
    except Exception as e:
        logger.debug(f"失衡跨档重挂检测: {e}")
    # 24h不成交自动重挂（系统自动做，不二次确认；撤单走安全保险只撤未成交单）
    _maybe_auto_rehang(st, client, pending_ids)
    # 每 tick 刷新展示字段（ATR%/密度/间距/挂单价），清除旧残留，前端永远显示当前真实值
    _refresh_grid_display(st)
    return


def _refresh_grid_display(st) -> None:
    """轻量刷新网格展示字段，供前端计算器读真实值。

    只更新 st 上的展示字段（atr_pct/spacing/density/upper/lower/anchor），
    不调交易所、不挂单、不撤单、不推送 —— 纯本地计算，无交易副作用。
    修复：挂单价/ATR%/密度/间距之前只在重挂时更新，残留旧值(如round(2)的0.01)。
    """
    try:
        # 用当前状态重算挂单价（锚=固定锚或成本中轴，间距=ATR%×密度，bePx钳制）
        # 与 place_grid_orders 里 calc_grid_levels 同一套逻辑，但静默不推送
        pos = st.position
        # 展示锚 = 固定锚；无则用多空开仓均价中轴(成本锚)。只读展示，绝不写回 grid_anchor_px。
        if st.grid_anchor_px > 0:
            anchor = st.grid_anchor_px
        else:
            la = pos.long_avg_px or 0.0
            sa = pos.short_avg_px or 0.0
            anchor = (la + sa) / 2 if (la > 0 and sa > 0) else (pos.last_px or pos.mark_px)
        atr = getattr(st, "atr_abs", 0.0) or 0.0
        # 展示密度 = 实际挂单用的档位密度（与 calc_grid_levels/place_grid_orders 一致，防止前端显示≠实际挂单）
        try:
            from formulas.safety import calc_imbalance_rate
            _imb_disp = calc_imbalance_rate(pos.long_contracts, pos.short_contracts)
            _density_disp = _ladder_density(st, _imb_disp)
        except Exception:
            _density_disp = _mode_base_density(st)
        if atr > 0 and anchor > 0:
            spacing = grid_spacing_pct(atr, anchor, _density_disp)
        else:
            spacing = getattr(st, "target_spacing_pct", 0.6) or 0.6
        upper = anchor * (1 + spacing / 100)
        lower = anchor * (1 - spacing / 100)
        st.grid_spacing_pct = round(spacing, 2)
        st.grid_upper_px = px_round(st.inst_id, upper)
        st.grid_lower_px = px_round(st.inst_id, lower)
        st.current_density = round(_density_disp, 3)
        # 注意：这里不写 st.grid_anchor_px —— 锚点只允许在 建仓/成交 时更新，防市价污染。
    except Exception as e:
        logger.debug(f"_refresh_grid_display: {e}")


def _maybe_auto_rehang(st, client, pending_ids) -> None:
    """网格挂单超时处理：
      - 12h 未成交 → 企业微信推送提醒一次（不自动改，等用户决策）
      - 24h 未成交 → 自动撤单重挂（纯系统行为，不二次确认）
    计时起点 = 最近一次成交时间 grid_last_trade_ts（每笔成交更新）；从未成交则用挂单时间。
    撤单走 _cancel_orders 安全保险（只撤未成交、只撤本引擎 clOrdId 的单）。
    """
    if not getattr(st, "grid_auto_run", True):
        return
    placed = getattr(st, "grid_placed_ts", 0.0) or 0.0
    if placed <= 0:
        return
    now = time.time()
    # 计时起点：最近成交时间，无则用挂单时间
    ref_ts = (getattr(st, "grid_last_trade_ts", 0.0) or 0.0) or placed
    elapsed = now - ref_ts

    # 12h 未成交 → 企微推一次（推过不重复；重新成交后 ref_ts 更新会重置计时）
    if elapsed >= 12 * 3600:
        last_push = getattr(st, "grid_12h_push_ts", 0.0) or 0.0
        # 距上次推送 > 11h（新周期）才再推，避免同周期反复刷屏
        if last_push <= 0 or (now - last_push) > 11 * 3600:
            try:
                from notify import notify
                inst = st.inst_id or ""
                now_px = st.position.last_px if getattr(st, "position", None) and st.position.last_px > 0 else st.position.mark_px
                notify("grid_stale",
                       f"⏰ 网格挂单 {elapsed/3600:.0f}h 未成交",
                       [f"{inst} 4方向限价单已挂 {elapsed/3600:.0f}h 无成交",
                        f"当前价: {now_px}  锚点: {st.grid_anchor_px}",
                        "请确认是否需要调整间距/参数"])
                st.grid_12h_push_ts = now
                save_state()
            except Exception as e:
                logger.warning(f"12h未成交提醒推送失败: {e}")

    # 24h 未成交 → 自动撤单重挂（纯系统行为，不二次确认）
    hours = float(getattr(st, "grid_rehang_hours", 24.0) or 24.0)
    cooldown = float(getattr(st, "grid_rehang_cooldown", 3600.0) or 3600.0)
    if elapsed < hours * 3600:
        return  # 未到24h
    # 距上次自动重挂不足冷却 → 跳过，防每轮反复重挂
    last_rehang = getattr(st, "grid_last_rehang_ts", 0.0) or 0.0
    if last_rehang > 0 and (now - last_rehang) < cooldown:
        return
    st.grid_last_rehang_ts = now
    _log(st, f"⏰ 网格挂单超 {hours:.0f}h 未成交 → 自动撤单重挂", cat="GRID")
    try:
        place_grid_orders(st)
    except Exception as e:
        _log(st, f"⚠ 24h自动重挂失败: {e}", level="ERROR", cat="GRID")
