# 小红书收藏夹人格分析 MVP

一个本地运行的小红书收藏分析原型。用户运行一行命令后打开本地网页，点击连接小红书，用本机浏览器登录并打开收藏页，再读取当前页面可见内容，生成搞笑、体面、可截图分享的收藏夹人格报告。

## 快速开始

```powershell
cd xhs-collection-persona
python app.py
```

如果你在 Windows 的 `cmd` 里启动，并且项目放在本仓库当前路径，可以直接运行：

```cmd
cd /d "E:\Codex Projects\Test\xhs-collection-persona"
start.bat
```

首次需要浏览器代理能力时安装依赖：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

然后重新运行：

```powershell
python app.py
```

本工作区已经准备了项目本地虚拟环境和项目内 Playwright Chromium。优先使用：

```cmd
start.bat
```

如果系统没有 Git，本项目也带了便携 Git：

```cmd
git-local.bat status
```

如果 GitHub App 或 Git 推送不可用，可以用本地上传脚本：

```cmd
set GITHUB_TOKEN=你的 GitHub fine-grained token
python upload_to_github.py
```

Token 只需要给当前仓库 `Contents: Read and write` 权限，不要把 token 发给任何人。

## 使用流程

1. 运行 `python app.py`，页面会自动打开。
2. 点击 `连接小红书`。
3. 在弹出的浏览器里登录小红书，并进入自己的收藏页。
4. 回到本地页面点击 `读取收藏`。
5. 点击 `生成画像` 查看报告。

## 隐私边界

- 不读取系统浏览器历史。
- 不要求用户输入小红书账号密码。
- 使用项目自己的本地浏览器配置目录保存登录态。
- 收藏数据和报告默认保存到本地 `data/app.db`。
- 配置 API Key 后，生成报告时会把采集到的文本摘要发送到用户配置的 OpenAI-compatible 接口；不配置时使用本地规则兜底。

## 参考方向

产品和技术方向参考了 [Trove AI](https://github.com/weaiw/trove-ai) 的几个思路：中文互联网内容采集、本地/自托管优先、OpenAI-compatible AI 配置、以及用户数据主权。当前项目仍保持极简 MVP，不引入 Docker、Next.js、PostgreSQL 或多用户体系。
