# Grid-V2.0 · 对冲网格（正式运行版本）

GridW2 对冲网格交易系统 —— 正式命名 **grid-V2.0**，纯 U本位/币本位 双向持仓网格对冲。

## 目录
```
grid-V2.0/
├── main.py              # FastAPI 入口 (端口 8001)
├── api/routes.py        # 全部 REST 路由 + 静态挂载(/W2/)
├── engine/              # 建仓/全平/对锁/调平/网格/趋势/同步
├── formulas/            # 公式层
├── data/                # 交易所数据 (exchange/ticker)
├── okx_client/          # OKX SDK 封装
├── monitor/             # 监控
├── static/              # 前端 (v3.html 单文件)
└── requirements.txt     # 依赖
```

## 本地直接运行
```bash
cd grid-V2.0
pip install -r requirements.txt
PORT=8001 python3 main.py
# 访问 http://tianjinsga.uunat.com/W2/ 或 http://localhost:8001/W2/
```

## Docker 运行
```bash
cd grid-V2.0
docker build -t grid-v2.0 .
docker run -d -p 8001:8001 -v /app/data:/app/data grid-v2.0
```

## 运行数据
- 持仓/网格状态：`/app/data/v2_grid_state.json`
- API 密钥：`/app/data/apikey.json`
- 诊断日志：`/app/data/logs/diag_*.jsonl`

（数据目录与代码分离，均在 `/app/data`，迁移/重启不丢数据）
