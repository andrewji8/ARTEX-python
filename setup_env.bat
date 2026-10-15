@echo off
echo 🚀 [执行 AI] 正在为 ARTEX Python 重构项目初始化环境...

:: 检查 Python 版本
python -c "import sys; assert sys.version_info >= (3, 11), '❌ 错误: 需要 Python 3.11+'"
if %errorlevel% neq 0 exit /b 1

:: 创建目录结构
mkdir src\api src\core src\db src\agents src\tools src\utils tests
type nul > src\__init__.py
type nul > src\api\__init__.py
type nul > src\core\__init__.py

:: 创建并激活虚拟环境
python -m venv .venv
call .venv\Scripts\activate.bat

:: 升级 pip
python -m pip install --upgrade pip setuptools wheel

:: 写入 requirements.txt (简化版，实际执行同上)
echo fastapi==0.111.0 > requirements.txt
echo uvicorn[standard]==0.30.1 >> requirements.txt
echo sqlalchemy[asyncio]==2.0.31 >> requirements.txt
echo asyncpg==0.29.0 >> requirements.txt
echo langgraph==0.1.5 >> requirements.txt
echo httpx==0.27.0 >> requirements.txt
echo pydantic==2.7.4 >> requirements.txt

:: 安装依赖
pip install -r requirements.txt

echo ✅ [执行 AI] 虚拟环境创建成功！
echo 👉 激活命令: .venv\Scripts\activate