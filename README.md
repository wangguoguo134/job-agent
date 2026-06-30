# Job-Agent：AI 求职助手

## 项目背景
本项目为腾讯产品经理训练营结课作业，目标是探索 AI Agent 在求职流程中的应用，包括岗位抓取、简历解析、JD 匹配、简历定制化生成与半自动投递辅助。

## 核心功能
1. 岗位信息抓取与结构化展示
2. 简历文本解析与候选人能力标签提取
3. JD-简历匹配评分
4. 基于岗位要求生成定制化简历建议
5. 通过 Playwright 辅助完成部分投递流程，最终投递由用户人工确认

## 技术实现
- 前端：HTML / CSS / JavaScript
- 后端：Flask
- 数据处理：Python
- 自动化：Playwright
- AI 辅助开发：Claude Code / Vibe Coding

## 产品亮点
- 将求职流程拆解为“岗位发现—匹配分析—简历优化—投递辅助”的完整闭环
- 强调人工确认机制，避免全自动误投递
- 具备从产品需求拆解到 MVP 实现的完整实践过程
- 将岗位匹配结果、技能缺口和简历微调结果放在同一流程中展示，便于用户快速决策

## 敏感信息处理
- 仓库不包含个人简历、投递结果、生成后的定制化简历等本地运行数据
- 已移除招聘链接中的推荐参数、token 等非必要信息
- `job_agent_data/` 为本地运行时目录，已加入 `.gitignore`

## 如何运行
```bash
pip install -r requirements.txt
playwright install chromium
python job-agent-server.py
```

后端启动后，打开 `job-agent-demo.html` 使用前端页面。
