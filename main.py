"""
main.py — FastAPI 入口（新架构 backend_v2）
启动：注册路由 + 后台初始化数据层 + 启动数据循环
"""
import os
import asyncio
import logging
from fastapi import FastAPI

from api.routes import register_routes
from data.ticker import init_adapter
from engine.sync import build_auth_client, refresh_available_instruments
from engine.tick import data_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

app = FastAPI()
register_routes(app)  # 静态文件挂载在 register_routes 内（/static + GET /）


@app.on_event("startup")
async def startup():
    from diagnostic_logger import get_diag_logger, RETAIN_DAYS
    diag = get_diag_logger()
    from datetime import datetime
    diag.info("START", f"🚀 服务启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                       f"(日志保留{RETAIN_DAYS}天, 分类: {len(diag.get_categories())}个)",
              {"retain_days": RETAIN_DAYS, "categories": list(diag.get_categories().keys())})

    loop = asyncio.get_event_loop()

    def _init(mech, fn):
        try:
            fn()
            diag.log_mechanism_start(mech, "初始化完成")
        except Exception as e:
            diag.error("START", f"❌ [{mech}] 初始化失败: {e}", {"mech": mech}, mech=mech)

    loop.run_in_executor(None, _init, "数据层", init_adapter)
    loop.run_in_executor(None, _init, "认证客户端", build_auth_client)
    loop.run_in_executor(None, _init, "合约列表", refresh_available_instruments)
    asyncio.create_task(data_loop())
    diag.info("START", "主数据循环已启动")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, log_level="info")
