"""
诊断级日志系统 — 双输出（内存 + JSONL文件）
用于事后分析故障和逻辑错误，全中文记录。

8 分类：ZONE / CALC / TRADE / OI / PARAM / DATA / ERROR / STATE
4 级别：DEBUG < INFO < WARN < ERROR

功能：
  - 文件按天滚动持久化（diag_YYYY-MM-DD.jsonl），启动时清理7天前的旧文件（保留≥7天）
  - 内存最近 MAX_MEM 条供前端实时查看
  - 机制健康状态：记录各机制(网格/调平/全平/风控/冰山/OI)的启动/心跳/错误，前端可看健康看板
  - 启动日志：服务启动时间、各模块初始化状态
  - 交易日志：建仓/平仓/调平/成交等交易事件（含方向/张数/价格/盈亏）
"""
from __future__ import annotations
import json
import os
import time
import threading
from collections import deque
from datetime import datetime

# ── 分类定义 ──
CATEGORIES = {"ZONE", "CALC", "TRADE", "OI", "PARAM", "DATA", "ERROR", "STATE",
              "START", "HEALTH", "MECH", "TRADE_EVENT"}

# ── 分类中文标签（前端显示用）──
CAT_LABELS = {
    "ZONE":  "区间",
    "CALC":  "计算",
    "TRADE": "交易",
    "OI":    "OI",
    "PARAM": "参数",
    "DATA":  "数据",
    "ERROR": "异常",
    "STATE": "状态",
    "START": "启动",
    "HEALTH": "健康",
    "MECH":  "机制",
    "TRADE_EVENT": "交易事件",
}

# ── 级别排序 ──
LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}

# ── 日志保留天数（≥7天）──
RETAIN_DAYS = int(os.environ.get("LOG_RETAIN_DAYS", "7"))


class DiagnosticLogger:
    """
    诊断日志器
    - 内存：最近 MAX_MEM 条，供前端实时查看（带分类/级别过滤）
    - 文件：JSONL 按天滚动，持久化到 LOG_DIR，用于事后分析
    - 保留：启动时清理 RETAIN_DAYS 天前的旧日志文件
    - 健康：各机制启动/心跳/错误状态聚合
    """

    def __init__(self, log_dir: str = "", max_mem: int = 2000):
        self.log_dir = log_dir or os.environ.get("LOG_DIR", "/app/data/logs")
        self.max_mem = max_mem
        self._mem: deque[dict] = deque(maxlen=max_mem)
        self._lock = threading.Lock()
        self._file = None
        self._file_date = ""
        # 机制健康状态: {机制名: {started, last_heartbeat, ok, errors, last_error, info}}
        self._health: dict[str, dict] = {}
        os.makedirs(log_dir, exist_ok=True)
        self._cleanup_old_files()

    def _cleanup_old_files(self):
        """删除 RETAIN_DAYS 天前的 diag_*.jsonl，保证日志至少保留 N 天。"""
        try:
            now = datetime.now()
            for fn in os.listdir(self.log_dir):
                if not fn.startswith("diag_") or not fn.endswith(".jsonl"):
                    continue
                # diag_2026-08-01.jsonl
                date_part = fn[len("diag_"):-len(".jsonl")]
                try:
                    fdate = datetime.strptime(date_part, "%Y-%m-%d")
                except ValueError:
                    continue
                age_days = (now - fdate).days
                if age_days > RETAIN_DAYS:
                    try:
                        os.remove(os.path.join(self.log_dir, fn))
                        self.info("DATA", f"清理旧日志文件(超{RETAIN_DAYS}天): {fn}", {"days": age_days})
                    except Exception:
                        pass
        except Exception:
            pass

    def _health_register(self, mech: str, ok: bool = True, err: str = ""):
        """更新机制健康状态。mech=机制名(网格/调平/全平/风控/冰山/OI等)。"""
        with self._lock:
            h = self._health.get(mech)
            if h is None:
                h = {"mech": mech, "started": time.strftime("%H:%M:%S"),
                     "last_heartbeat": time.strftime("%H:%M:%S"),
                     "ok": ok, "errors": 0, "last_error": err or "", "info": ""}
                self._health[mech] = h
            else:
                h["last_heartbeat"] = time.strftime("%H:%M:%S")
                if not ok:
                    h["errors"] = h.get("errors", 0) + 1
                    h["last_error"] = err or ""
                    h["ok"] = False
                else:
                    h["ok"] = True

    def log(self, level: str, cat: str, msg: str, data: dict | None = None,
            mech: str | None = None):
        """
        记录一条日志
        level: DEBUG / INFO / WARN / ERROR
        cat:   ZONE / CALC / TRADE / OI / PARAM / DATA / ERROR / STATE / START / HEALTH / MECH / TRADE_EVENT
        msg:   中文消息
        data:  附加结构化数据（可选）
        mech:  机制名（可选，用于健康状态聚合）
        """
        if cat not in CATEGORIES:
            cat = "STATE"
        if level not in LEVEL_ORDER:
            level = "INFO"

        now = time.time()
        ts = datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S.") + \
             f"{int(now*1000)%1000:03d}"

        entry = {
            "ts": ts,
            "level": level,
            "cat": cat,
            "msg": msg,
        }
        if data is not None:
            # 清理 data 中不可序列化的值
            clean = {}
            for k, v in data.items():
                try:
                    json.dumps(v)
                    clean[k] = v
                except (TypeError, ValueError):
                    clean[k] = str(v)
            entry["data"] = clean
        if mech:
            entry["mech"] = mech
            self._health_register(mech, ok=(level != "ERROR"), err=(msg if level == "ERROR" else ""))

        with self._lock:
            # 内存
            self._mem.append(entry)
            # 文件
            self._write_file(entry)

    def debug(self, cat: str, msg: str, data: dict | None = None, mech: str | None = None):
        self.log("DEBUG", cat, msg, data, mech)

    def info(self, cat: str, msg: str, data: dict | None = None, mech: str | None = None):
        self.log("INFO", cat, msg, data, mech)

    def warn(self, cat: str, msg: str, data: dict | None = None, mech: str | None = None):
        self.log("WARN", cat, msg, data, mech)

    def error(self, cat: str, msg: str, data: dict | None = None, mech: str | None = None):
        self.log("ERROR", cat, msg, data, mech)

    def log_health(self, mech: str, ok: bool, detail: str = "", data: dict | None = None):
        """记录机制健康事件（启动/心跳/异常），供前端健康看板。"""
        level = "INFO" if ok else "ERROR"
        cat = "HEALTH" if ok else "ERROR"
        self.log(level, cat, f"[{mech}] {'正常' if ok else '异常'}: {detail}", data, mech=mech)

    def log_mechanism_start(self, mech: str, detail: str = "", data: dict | None = None):
        """记录机制启动（含启动时间/初始化状态）。"""
        self.log("INFO", "MECH", f"🟢 机制启动 [{mech}]: {detail}", data, mech=mech)

    def log_mechanism_stop(self, mech: str, detail: str = "", data: dict | None = None):
        """记录机制停止。"""
        self.log("INFO", "MECH", f"⏹ 机制停止 [{mech}]: {detail}", data, mech=mech)

    def log_trade(self, action: str, side: str = "", sz: float = 0, px: float = 0,
                  pnl: float = None, detail: str = "", data: dict | None = None):
        """记录交易事件（建仓/平仓/调平/成交），合并进诊断日志，前端📋弹窗统一显示。"""
        pnl_txt = f" 盈亏={pnl:.2f}" if pnl is not None else ""
        msg = f"💰 交易[{action}] {side} {sz}张 @{px}{pnl_txt} {detail}"
        d = dict(data or {})
        d.update({"action": action, "side": side, "sz": sz, "px": px})
        if pnl is not None:
            d["pnl"] = pnl
        self.log("INFO", "TRADE_EVENT", msg, d, mech=action)

    def get_logs(self, cat: str | None = None, level: str | None = None,
                 limit: int = 200) -> list[dict]:
        """
        获取日志（支持过滤）
        cat:   按分类过滤（None=全部）
        level: 按最低级别过滤（None=全部, "INFO"=INFO及以上）
        limit: 返回条数上限
        """
        with self._lock:
            logs = list(self._mem)

        if cat:
            logs = [e for e in logs if e["cat"] == cat]
        if level and level in LEVEL_ORDER:
            min_ord = LEVEL_ORDER[level]
            logs = [e for e in logs if LEVEL_ORDER.get(e["level"], 0) >= min_ord]

        return logs[-limit:]

    def get_health(self) -> list[dict]:
        """返回各机制健康状态（前端健康看板用）。"""
        with self._lock:
            return list(self._health.values())

    def get_categories(self) -> dict[str, str]:
        """返回分类标签映射"""
        return dict(CAT_LABELS)

    def get_today_file(self) -> str:
        """返回当天日志文件路径"""
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"diag_{today}.jsonl")

    def get_file_logs(self, date: str | None = None, limit: int = 500) -> list[dict]:
        """从文件读取日志"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, f"diag_{date}.jsonl")
        if not os.path.exists(path):
            return []
        logs = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            logs.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return logs[-limit:]

    def _write_file(self, entry: dict):
        """写入 JSONL 文件（按天滚动）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._file_date:
            # 切换日期，关闭旧文件
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            self._file_date = today
            path = os.path.join(self.log_dir, f"diag_{today}.jsonl")
            try:
                self._file = open(path, "a", encoding="utf-8")
            except Exception:
                self._file = None

        if self._file:
            try:
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
            except Exception:
                pass


# ── 全局单例 ──
_diag: DiagnosticLogger | None = None


def get_diag_logger(log_dir: str = "") -> DiagnosticLogger:
    global _diag
    if _diag is None:
        _diag = DiagnosticLogger(log_dir=log_dir)
    return _diag

