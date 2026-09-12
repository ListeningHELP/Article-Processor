# 文章处理助手

> 本地运行的文章工作台：写作、翻译、章节编辑、文本工具，全部基于 DeepSeek API，纯 Python 标准库实现，零第三方依赖。

> ⚠️ 需手动接入 DeepSeek API：在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 创建 Key，填入 `config.json` 即可使用（按量付费，价格低）。

## 功能一览

| 模块 | 功能 |
|---|---|
| 写作 | 活动总结、推文、报告、会议总结、自我介绍、PPT 演讲稿、润色 |
| 翻译 | 长文本自动分块翻译、批量导入文档、术语表、学术 / 通俗小说双风格 |
| 章节编辑 | 导入 TXT / EPUB / MOBI，按规则拆章，导出 EPUB / TXT / MOBI |
| 文本工具 | 合并 TXT（含文件夹）、网页转 TXT、批量转 PDF |
| 额度显示 | 侧边栏实时显示 DeepSeek 账户余额 |

## 快速开始

1. **获取 API Key**：在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 注册并创建（按量付费，价格低）
2. **配置**：复制 `config.example.json` 为 `config.json`，填入 Key：
   ```json
   { "api_key": "sk-你的密钥", "model": "deepseek-chat", "temperature": 0.7 }
   ```
3. **启动**：双击 `start.bat`（服务后台运行，黑框自动关闭，浏览器自动打开 `http://127.0.0.1:8234`）
4. **使用**：侧边栏切换模块

> 服务已在运行时再次双击 `start.bat`，只会打开浏览器，不会重复启动。

### 如何停止服务

服务在后台静默运行。停止方法（任选其一）：

- **方法一**：任务管理器 → 详细信息 → 找到 `pythonw.exe`（或 `python.exe`）→ 结束任务
- **方法二**：命令行执行：
  ```
  netstat -ano | findstr ":8234 " | findstr "LISTENING"
  taskkill /PID 上一步显示的进程号 /F
  ```

## 模块说明

### 写作

一句话描述需求即可生成。内置快捷指令：团课总结、团课推文、活动报告、会议总结、自我介绍、PPT 演讲、润色。粘贴已有文字后可直接说"润色"、"改写成更正式的公文风格"。

### 翻译

- **直接粘贴**：整章小说自动分块翻译，人名地名前后一致
- **批量导入**：一次选择多个 TXT / EPUB / MOBI，网盘式队列逐个翻译，可单独或全部下载
- **双风格**：学术（论文、报告）/ 通俗小说（默认）
- **术语表**：每行一条 `原名 = 译名`，全篇统一遵守
- **超长文档**：几十万字自动拆块翻译，单块失败自动重试跳过，不中断

### 章节编辑

- 批量导入 TXT / EPUB / MOBI，逐个打开编辑
- 自动拆章：中文章节、英文章节、卷/Part、序/前言/后记/附录、自定义正则、按长度均分
- 支持改名、预览、删除章节
- 导出：EPUB（标准 EPUB3 带目录）/ TXT / MOBI

### 文本工具

| 功能 | 说明 |
|---|---|
| 合并 TXT | 多选文件按序合并；或选文件夹，按名称自然排序合并全部 TXT |
| 网页转 TXT | 选 HTM/HTML 文件或文件夹，批量提取正文为纯文本 |
| 批量转 PDF | 选 TXT 文件或文件夹，由本机 Word 排版生成 PDF |

### 依赖

| 功能 | 依赖 |
|---|---|
| 全部核心功能 | 仅 Python 3.10+（Windows 自带 Git Bash 环境即可） |
| 批量转 PDF | 本机安装 Microsoft Word |
| 导出 MOBI | 可选，Amazon kindlegen；未安装时可用 EPUB / TXT 替代 |

## 常见问题

| 问题 | 解决 |
|---|---|
| 页面打不开 | 确认 `pythonw.exe` 在任务管理器中运行；双击 start.bat 看是否提示错误 |
| 提示未配置 Key | 检查 `config.json` 是否存在、`api_key` 是否为空 |
| Key 无效（401） | Key 是否完整（`sk-` 开头）；重新生成后更新 config.json |
| EPUB 章节错乱 | 该 EPUB 无标准目录，改用「自定义正则」或「按长度均分」重新拆分 |

## 隐私说明

- 请求直连 DeepSeek 官方 API，本地不留存对话记录（会话历史仅存浏览器本地）
- API Key 仅保存在本机 `config.json`，已被 `.gitignore` 排除，不会上传
