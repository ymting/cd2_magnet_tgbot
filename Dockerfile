# 使用轻量级 Python 3.13 镜像作为基础
FROM python:3.13-slim

# 设置容器内工作目录
WORKDIR /app

# 设置环境变量，确保 Python 输出直接打印到日志
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 复制依赖清单并安装
# 这样即便代码变了，只要依赖没变，构建时就会利用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制当前目录下所有项目文件（包含 main.py 和 pb2 相关文件）
COPY . .

# 赋予程序运行权限（可选）
RUN chmod +x main.py

# 启动程序
CMD ["python", "main.py"]