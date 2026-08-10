# 网格无损接管 + clOrdId 归属标记 · 设计文档

> 状态：待确认实施
> 日期：2026-08-10
> 背景：多实例连同一 OKX 账户导致"running 标志与交易所挂单脱节"（本机 grid-V2.0 在跑、首尔显示停止但交易所单在动）。目标：让引擎能自愈式从交易所"认领"自己的挂单，彻底告别"改 state + 重启容器"。

---

## 一、目标

1. **挂单带归属标记**：引擎挂的所有限价单带 `clOrdId` 前缀，用于区分"我的单 / 幽灵单"。
2. **自愈式认领**：引擎启动/恢复/手动 recover 时，从交易所拉当前挂单，按前缀**认领自己的单**（不撤单、无损接管），重建 `grid_upper/lower_ord_ids`。
3. **防幽灵单**：不带标记的单不收养、报警提示，不默默掩盖多实例/手动单问题。
4. **前端一键恢复**：提供 `POST /api/v1/grid/recover`，无需 SSH + 改文件 + 重启容器。

---

## 二、改动点（4处）

### 改动1：`engine/grid.py` `place_grid_orders()` — 挂单加 clOrdId

在 orders 字典（4单或2单）里加 `clOrdId`：

```python
# 生成器：gw2seo + 时间戳 + 单序号（每单唯一，≤32位）
def _gen_clordid(idx: int) -> str:
    return f"gw2seo{int(time.time()*1000)%10000000000}{idx}"  # 例 gw2seo12345678900
```

- 前缀 `gw2seo`：机器标识（防跨部署碰撞）。不同部署用不同前缀。
- 唯一性：`时间戳+序号` 保证当前挂单唯一（OKX 要求 live 单唯一）。
- 上端/下端、单向分支的每张单都加，idx 递增。

### 改动2：`engine/grid.py` `check_grid_tick()` 启动分支 — 从"撤所有"改"按前缀认领"

**现状**（破坏性）：
```python
if not up and not lo:
    if pending_ids:
        client.cancel_all_pending(st.inst_id)  # 撤交易所所有单
    place_grid_orders(st)
    return
```

**改为**（自愈认领）：
```python
if not up and not lo:
    _adopt_existing_orders(st, client, pending)  # 认领，见下
    # 若认领后仍无单 → 正常新建
    up, lo = st.grid_upper_ord_ids, st.grid_lower_ord_ids
    if not up and not lo:
        place_grid_orders(st)
    return
```

**新增 `_adopt_existing_orders(st, client, pending)`**：
```python
def _adopt_existing_orders(st, client, pending):
    """按 clOrdId 前缀认领交易所现有挂单。幂等；失败不误撤不误认。"""
    own = [o for o in pending if str(o.get("clOrdId","")).startswith("gw2seo")]
    others = [o for o in pending if not str(o.get("clOrdId","")).startswith("gw2seo")]
    if others:
        _log(st, f"⚠️ 发现 {len(others)} 笔非本引擎挂单(无标记)，不收养，请人工确认: "
                 f"{[o.get('ordId') for o in others]}", level="WARN")
    if not own:
        return False
    # 按 side 分组：sell→上端(平多+开空)，buy→下端(平空+开多)
    st.grid_upper_ord_ids = [o["ordId"] for o in own if o.get("side")=="sell"]
    st.grid_lower_ord_ids = [o["ordId"] for o in own if o.get("side")=="buy"]
    save_state()
    _log(st, f"🔁 认领现有挂单: 上{st.grid_upper_ord_ids} 下{st.grid_lower_ord_ids}", cat="GRID")
    return True
```

- **幂等**：认领后 `grid_upper/lower` 有值，下一轮 check_grid_tick 走"有挂单记录"分支，不会重复认领。
- **失败兜底**：`pending` 查询异常时走原 try/except，保持现状，下轮重试。

### 改动3：`engine/build.py` `execute_start_grid()` 恢复分支 — 接入认领

**现状**（恢复运行只置 running=True，不认领 → 下一轮破坏）：
```python
if st.position.long_contracts > 0 or st.position.short_contracts > 0:
    st.running = True
    st.pending_iceberg = 0
    save_state()
    return {"status": "ok", "data": {...}, "resumed": True}
```

**改为**：置 running=True 后，主动触发一次认领（拉 pending 认领），再 save_state。
```python
if st.position.long_contracts > 0 or st.position.short_contracts > 0:
    st.running = True
    st.pending_iceberg = 0
    try:  # 认领现有挂单，避免下轮 check_grid_tick 破坏性撤单
        from engine.grid import adopt_or_reset_grid_orders
        adopt_or_reset_grid_orders(st)
    except Exception as e:
        _log(st, f"⚠️ 恢复时认领失败: {e}", level="WARN")
    save_state()
    return {"status": "ok", "data": {...}, "resumed": True}
```

### 改动4：`api/routes.py` — 加 `POST /api/v1/grid/recover`

```python
@app.post("/api/v1/grid/recover")
async def recover_grid():
    st = get_state()
    from engine.grid import adopt_or_reset_grid_orders
    r = adopt_or_reset_grid_orders(st)   # 认领（或重建）
    st.running = True
    save_state()
    return {"status": "ok", "data": {"adopted": r}}
```

前端加"🔁 恢复接管"按钮（可选，或复用"开始"按钮）。

---

## 三、认领分组逻辑（关键）

| 单类型 | side | 归组 |
|:--|:--|:--|
| 上端（平多+开空）| sell | `grid_upper_ord_ids` |
| 下端（平空+开多）| buy | `grid_lower_ord_ids` |

- **单向模式**：只挂一组（重仓空→只下端 buy / 重仓多→只上端 sell），按 side 分组天然支持，不假设上下都有。
- **部分成交**：单仍 live/partially_filled，正常认领；`_verify_filled` 已处理后续。

---

## 四、连锁影响与边界

| 场景 | 处理 |
|:--|:--|
| 全平 `flat.py` | 维持 `cancel_all_pending` 撤所有（含幽灵单）——全平就该清干净 ✅（已拍板）|
| 切换合约 | 认领只查当前 `inst_id` 的 pending，旧合约单天然隔离 ✅ |
| 切换模式(模拟/实盘) | 认领按当前账户查 pending，实盘无单则正常新建 ✅ |
| 认领 API 异常 | try/except 包裹，不误撤不误认，记日志下轮重试 ⚠️ |
| clOrdId 前缀碰撞 | 用 `gw2seo`（机器标识），不同部署不同前缀 ⚠️ |
| 混合(带标记+幽灵) | 带标记接管，不带标记**报警不收养**，等你决定 ✅（已拍板）|
| 幂等 | 认领后 grid_upper/lower 有值，不重复认领 ✅ |

---

## 五、验证方案（改完必测）

1. **无损重启**：引擎挂单 → 重启容器 → 确认交易所单不撤、grid_upper/lower 重建、running=True。
2. **幽灵单**：手动挂一笔无标记单 → 确认报警、不收养、其余带标记单正常接管。
3. **单向**：构造失衡触发单向挂单 → 重启 → 认领只恢复挂的那组。
4. **全平**：确认撤所有限价单（含幽灵单）。
5. **前端一键**：点 recover 按钮 → 确认无损接管、running=True。
6. **node -c / 语法检查**：改完 JS 跑 `node -c`；Python 跑 `python -m py_compile`。
7. **git commit + tag**：改完必须提交（用户红线）。

---

## 六、需要用户最后确认的实施项

- [ ] clOrdId 前缀用 `gw2seo`（可改）
- [ ] 认领逻辑落地到 `_adopt_existing_orders`（或独立函数）
- [ ] recover 接口加前端按钮，还是复用"开始"
- [ ] 确认后开始改代码（本机开发 → git commit → 部署首尔容器）
