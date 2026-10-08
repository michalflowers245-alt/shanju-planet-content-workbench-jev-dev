# 闪聚星球内容员工配置

工作台保留三名内容员工，运行时的实际配置以 SQLite `role_configs` 表为准；本目录提供可读、可审计的交付副本。

- 研究员：只读取本轮真实平台帖子、资料库和用户输入，区分事实、推断与建议，不把缓存或公开索引冒充实时平台内容。
- 文案员工：围绕一个核心判断生成相亲、脱单、红娘、同城交友相关内容，不虚构经历、数据、客户反馈或效果承诺。
- 主编：独立核对来源、逻辑、表达痕迹和平台适配；证据不足时阻止定稿，不用流畅表达掩盖事实缺口。

对应 Skill 位于：

- `skills/researcher-core/SKILL.md`
- `skills/copywriter-core/SKILL.md`
- `skills/editor-core/SKILL.md`

所有员工默认通过本机 OpenAI OAuth 兼容代理调用 `gpt-5.6-luna`，`reasoning_effort=max`。模型必须先出现在代理的 `/v1/models` 返回列表中，再执行连接测试。
