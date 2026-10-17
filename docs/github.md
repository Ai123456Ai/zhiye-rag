# GitHub 仓库使用与发布

建议仓库名 `zhiye-rag`，简介可写“Python / FastAPI RAG 知识库，支持混合检索、可选重排序、版本筛选、证据引用与可复现评测”。求职展示通常使用公开仓库；实际目标和可见性由仓库所有者选择。

## 首次发布内容

上传应用、静态页面、测试、依赖文件、示例资料、评测题集、来源许可及评测报告。README 提供安装步骤、架构图和实际测量结果。

`.env` 及派生环境文件（`.env.example` 除外）、`data/`、`.venv/`、`.runtime/`、`reports/` 和 Python 缓存不上传。Git / GitHub CLI 的便携工具位于 `.runtime/tools/`，同样不会上传。评测留档中的本机路径用于记录当时运行条件，不是换电脑运行时需要照填的配置。

六篇 FastAPI 文档保留原有 MIT 许可及来源清单。上游文档的许可不能自动视作本项目全部代码的许可；当前没有为项目代码额外选择开源许可证。

## 自动检查

[github-actions-basic.yml](github-actions-basic.yml) 是可启用的自动检查模板。将其复制到 `.github/workflows/basic.yml` 并提交后，会在 push、pull request 或手动触发时执行 Windows / Python 3.12 的基础复现验证。它使用固定依赖、新虚拟环境和临时数据库，不配置模型 API 密钥、不下载本地模型。通过后上传 `windows-basic-reproduction` 结果文件。

当前模板尚未启用或在 GitHub 运行，不能声称 GitHub Actions 已通过。上传工作流需要 GitHub OAuth 的 `workflow` 权限；首次发布保留为文档模板，使用现有仓库权限上传项目。之后可在授权齐全时启用。基础测试缺少可选模型依赖时跳过一项，这是已记录的验证范围；GPU 和 Docker 仍需另行验证。GitHub 托管运行环境的 Python 补丁版本可能与本机不同，结果文件会记录实际版本。

## 发布和登录

需要已登录的 GitHub 账户以及明确的目标仓库。使用浏览器或 `gh auth login --web` 完成登录；不要把访问令牌写入项目、聊天或提交记录。

标准 Git 和 GitHub CLI 在 PATH 中时，可先检查：

```powershell
git status --short
git diff --cached --stat
git ls-files
gh auth status
```

确认目标后，已有远程仓库可配置 origin 并推送；空仓库可用 `gh repo create` 建立并推送。目标 URL、公开或私有状态和提交作者应来自真实账户信息，不能根据电脑用户名猜测。如果已存在 origin 或远端提交，应先检查，不强制覆盖远端历史。

若尚未登录，在本机完成登录后继续推送即可；GitHub 账户的交互式登录需要账户所有者操作。提交前检查暂存内容，确认 `.env.example` 包含空密钥、正文和历史中均无真实密钥。忽略规则不会自动移除已经进入历史的文件。
