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
        from notify import on_trade
        pos = st.position
        px = getattr(st, f"grid_{side}_px", 0) or 0
        n = _grid_step_contracts(st)
        on_trade(desc, n, round(px, 2), round(st.total_equity or 0, 2),
                 pos.long_contracts, pos.short_contracts)
    except Exception as e:
        logger.debug(f"notify_trade: {e}")


def _mode_base_density(st) -> float:
    """模式base密度：进攻=base_density，防守=defense_density（文档§5.1）。"""
    if getattr(st, "mode", "attack") == "defense":
        return float(getattr(st, "defense_density", 2.0) or 2.0)
    return float(getattr(st, "base_density", 2.0) or 2.0)


def _ladder_density(st, imbalance) -> float:
    """档位表密度：取失衡率对应档（≥该档取该档密度），未达档位取 base。文档§2.3。"""
    rates = list(getattr(st, "ladder_rates", [40, 50, 60, 70, 80]) or [40, 50, 60, 70, 80])
    dens = list(getattr(st, "ladder_densities", [1.4, 1.0, 0.75, 0.5, 0.3]) or [1.4, 1.0, 0.75, 0.5, 0.3])
    d = float(getattr(st, "base_density", 2.0) or 2.0)
    for r, dd in zip(rates, dens):
        if imbalance >= r:
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
    anchor = pos.last_px if pos.last_px > 0 else pos.mark_px  # 成交价锚，无则回退标记价

    imbalance = calc_imbalance_rate(pos.long_contracts, pos.short_contracts)
    heavy, _ = _heavy_side(st)
    # 无持仓或完全对冲 → 不判定状态，用模式base密度、bePx夹逼
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
        if emergency:
            density = min(_mode_base_density(st), _ladder_density(st, imbalance))
        else:
            density = _mode_base_density(st)

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

    # 记录展示
    st.grid_spacing_pct = round(spacing, 2)
    st.grid_upper_px = round(upper, 2)
    st.grid_lower_px = round(lower, 2)
    st.current_density = round(density, 3)
    st.emergency_state = emergency
    st.heavy_side = heavy
    st.heavy_state = heavy_state
    return upper, lower


def _cancel_orders(st, ord_ids: list) -> None:
    """按 ordId 精确撤销剩余网格单（不误伤其它单）。"""
    if not ord_ids:
        return
    from data.exchange import get_auth_client
    client = get_auth_client()
    if client is None:
        return
    for oid in ord_ids:
        try:
            client.cancel_order(st.inst_id, oid)
        except Exception as e:
            logger.debug(f"撤单 {oid} 异常: {e}")


def _verify_filled(client, inst_id: str, ord_ids: list) -> tuple[bool, dict]:
    """逐个核实 ordId 的真实状态，判断是否真正成交。

    背景：grid.py 原判定把"pending 查询结果中缺单/空"直接当"成交"，网络抖动时
    pending 查询可能返回空 → 误判成交 → 误撤。这里改为逐单查交易所真实 state：
      - filled            → 真成交
      - canceled          → 被撤（非成交，诊断）
      - live/空           → 仍在挂单中（pending查询延迟/异常）→ 不算成交
      - 查询抛异常/无数据 → 无法确认 → 不算成交，由调用方跳过本轮
    返回 (是否确认真成交, 各单state明细)。任何一单查询失败返回 (False, {})。
    """
    states: dict[str, str] = {}
    for oid in ord_ids:
        try:
            o = client.get_order(inst_id, oid)
        except Exception as e:
            logger.warning(f"核实成交状态失败 {oid}: {e} → 本轮跳过，不判定成交")
            return False, {}
        stt = (o or {}).get("state", "") if isinstance(o, dict) else ""
        if not stt:
            logger.warning(f"核实成交状态无数据 {oid} → 本轮跳过，不判定成交")
            return False, {}
        states[oid] = stt
    filled = all(states.get(oid) == "filled" for oid in ord_ids) if ord_ids else False
    return filled, states


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
    save_state()
    _log(st, f"📊 挂单: 上{upper:.1f}(平多+开空)/下{lower:.1f}(平空+开多) 各{n}张"
             f" (密度{getattr(st, 'current_density', 2.0)}, 单向={one_way})",
         data={"upper": upper, "lower": lower, "n": n,
               "upper_ids": upper_ids, "lower_ids": lower_ids,
               "density": getattr(st, "current_density", 2.0), "one_way": one_way})
    return {"status": "ok", "data": {"upper": upper, "lower": lower, "n": n}}


def check_grid_tick(st) -> None:
    """网格主流程：无单→挂单；单成交(ordId消失)→撤对侧+重挂。

    事件驱动：挂单组 ordId 不再出现在 pending 里 = 该方向成交（价格穿过了挂单价）。
    成交本身由限价单自动完成（平多开空/平空开多），这里只负责撤对侧 + 以新成交价重挂。
    """
    if not st.running or st.paused:
        return
    if not getattr(st, "auto_adjust", True):
        return
    from data.exchange import get_auth_client
    client = get_auth_client()
    if client is None:
        return

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
        filled_up, states_up = _verify_filled(client, st.inst_id, up_missing)
        if not filled_up:
            # 无法确认成交（查询失败/仍在挂单/被撤）→ 跳过本轮，记诊断日志，不误撤
            logger.warning(
                f"上端单疑似变动但未确认真成交: missing={up_missing} "
                f"states={states_up} pending空={not pending_ids} → 跳过本轮"
            )
            return
        _log(st, f"🔺 上端成交(平多+开空) → 撤下端, 重挂", cat="GRID")
        _notify_trade(st, "平多/开空", "upper")
        if lo:
            _cancel_orders(st, [oid for oid in lo if oid in pending_ids])
        st.grid_upper_ord_ids = []
        st.grid_lower_ord_ids = []
        save_state()
        place_grid_orders(st)
        return

    # 下端可能成交：核实
    if lo_missing:
        filled_lo, states_lo = _verify_filled(client, st.inst_id, lo_missing)
        if not filled_lo:
            logger.warning(
                f"下端单疑似变动但未确认真成交: missing={lo_missing} "
                f"states={states_lo} pending空={not pending_ids} → 跳过本轮"
            )
            return
        _log(st, f"🔻 下端成交(平空+开多) → 撤上端, 重挂", cat="GRID")
        _notify_trade(st, "平空/开多", "lower")
        if up:
            _cancel_orders(st, [oid for oid in up if oid in pending_ids])
        st.grid_upper_ord_ids = []
        st.grid_lower_ord_ids = []
        save_state()
        place_grid_orders(st)
        return

    # 两侧都在挂单等待中，什么都不做
