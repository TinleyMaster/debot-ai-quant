# 使用 Playwright 官方预装镜像（已含 Python + Chromium + 所有系统依赖）
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# 安装额外的 Python 依赖（Playwright 已预装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建持久化数据目录
RUN mkdir -p /app/data

# 健康检测端口
EXPOSE 8080

# 主程序（Playwright 官方镜像已包含所有显示库，无需 xvfb）
CMD ["python", "main.py"]
