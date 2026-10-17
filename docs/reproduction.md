# 基础环境复现验证

本轮验证基础版能在不共享全局库、不含现有 `.env`、数据库和模型权重的代码副本中运行。验证在同一台 Windows 电脑的新环境完成，不等于已在另一台电脑或其他操作系统测试。

## 新目录安装

使用 Python 3.12，在下载的项目根目录执行：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-repro-win-py312.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

首次下载后的基础模式无需 `.env`。访问 http://127.0.0.1:8000，导入演示手册，再提问“北京出差住宿报销上限是多少？”。没有模型 API 配置时显示原文摘录，这是基础模式的预期行为。

已有 `.venv` 不要直接重建；可以改用 `.runtime/repro-venv`，并相应调整命令中的解释器路径。`pyvenv.cfg` 应包含 `include-system-site-packages = false`。已有服务占用 8000 时，手动演示可选择其他端口；自动验证会自行选择空闲端口。

需要模型生成时，复制 `.env.example` 为 `.env` 并填写 API 地址、密钥和服务商实际支持的模型名称，保存后重启服务。已有 `.env` 不要覆盖。复制出的示例默认没有启用生成或语义模型；缓存路径是 `data/models/cache`，数据库是 `data/knowledge.db`，命令从项目根目录运行。

## 自动复现

在仅安装上述固定基础依赖的独立环境执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_reproduction.py
```

该脚本拒绝共享全局库、已安装 PyTorch / NumPy / Sentence Transformers 或依赖版本不一致的环境。它不会自动安装依赖。已有报告拒绝覆盖，另一次运行可使用 `--output reports/reproduction-second.json`。

验证过程包括：

1. 将应用、静态页面、示例、题集、测试和评测留档复制到临时目录；不复制 `.env`、`data`、`.venv`、`.runtime` 和 Git 目录。
2. 清除验证子进程继承的模型配置，显式关闭生成、本地 Embedding 和重排序；使用临时数据库。
3. 检查依赖，运行全部测试。模型接口相关用例使用模拟接口，不请求真实服务。
4. 实际启动 Uvicorn，检查页面、静态资源、OpenAPI、空库、上传、去重、BM25、问题拆解和摘录回答。
5. 停止并重新启动临时服务，确认文档和检索来源保留，再删除文档确认片段清除。
6. 校验公开资料来源与许可，运行既有 FastAPI dev 10 题的 BM25 检索评测。

结束后停止临时服务，临时副本和数据库随目录清理。当前运行中的服务、用户知识库和配置不参与验证。报告记录依赖版本、复制文件哈希、测试输出、HTTP 检查及检索结果。

## 本机实测

原始证据保存于 [reproduction-run.json](reproduction-run.json)。环境：Windows x64，Python 3.12.10，`include-system-site-packages = false`；没有安装 PyTorch、NumPy 或 Sentence Transformers。

| 检查 | 结果 |
| --- | --- |
| 依赖一致性 | `pip check` 通过，已安装版本与固定依赖文件一致 |
| 自动测试 | 76 passed、1 skipped |
| HTTP 功能与持久化 | 8 组检查通过 |
| 公开资料校验与 BM25 dev 评测 | 完成，原始逐题数据保存在 JSON 中 |
| 真实生成 API | 未调用，0 次 |
| 现有知识库 | 未使用 |

跳过的用例是 `test_local_reranker_uses_query_text_pairs_and_raw_scores_without_network`，需要可选 NumPy / PyTorch；基础版未安装这些包。其余使用替代编码器和重排器的逻辑测试照常运行，不能据此宣称已在干净环境验证 GPU 模型推理。

首次检查发现两项测试在新版 Starlette 下模拟范围过大，拦截了测试客户端自身的请求；已改为保留测试客户端请求，只模拟外部模型请求。应用代码和历史评测记录没有因此更改。新版 Starlette 还会提示未来迁移测试客户端至 httpx2，本次测试仍可运行，该提示保留在原始输出中。

## 依赖和验证范围

`requirements.txt`、`requirements-dev.txt` 保留版本范围；`requirements-repro-win-py312.txt` 记录本次实际安装的基础与测试依赖精确版本，不含 GPU 依赖。固定版本便于复现，不包含下载文件哈希，也不保证未来软件源一直提供这些版本。升级依赖后需重新验证，不能沿用本次结果。

本轮没有在 Linux、macOS、Docker 或云服务器实测，也没有重新跑真实模型回答、GPU 或 CPU 语义模型推理。Docker 的基础镜像不含模型依赖，使用本机 `.env` 中的 Windows 权重路径无法直接复现。

基础 CPU 模式只使用 BM25，不需下载模型。若需要在 CPU 上使用向量或重排序，再安装 `requirements-models.txt`、准备权重，将对应设备设为 `cpu`，随后另外验证。GPU 安装要按实际显卡和驱动选择 PyTorch 发行版；本机共享的全局 CUDA PyTorch 不属于基础依赖文件。

## 上传 GitHub 前

保留代码、固定依赖、`.env.example`、题集、来源许可和评测报告。`.gitignore` 已排除 `.env` 及其派生配置（保留 `.env.example`）、`data/`、`.venv/`、`.runtime/`、缓存和临时 `reports/`。本轮未创建远程仓库或上传内容；提交前还需要用实际 Git 暂存区确认文件清单，排除规则不能替代 Git 历史检查。
