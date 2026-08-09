FROM python:3.11-slim

WORKDIR /app

# 安装工具链（dig 用于绕开 DNS 毒化 + tzdata 用于时区同步）
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends dnsutils tzdata && rm -rf /var/lib/apt/lists/*

# 时区设为香港（UTC+8），与交易所显示、K线图统一
ENV TZ=Asia/Hong_Kong

# 运行数据目录（状态/API密钥/日志持久化，与代码包 data/ 分离）
ENV DATA_DIR=/app/data-run
ENV V2_STATE_FILE=/app/data-run/v2_grid_state.json
ENV LOG_DIR=/app/data-run/logs
ENV WECOM_SESSION_FILE=/app/data-run/wecom_session.json
ENV WECOM_LOG_FILE=/app/data-run/wecom_link.log
RUN mkdir -p /app/data-run/logs

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health', timeout=3)"

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
