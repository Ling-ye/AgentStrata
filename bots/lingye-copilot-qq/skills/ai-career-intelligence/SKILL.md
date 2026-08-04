---
name: AI 岗位与面试情报
description: 搜索目标公司的 AI/Agent 后端岗位，研究薪资待遇和真实面经，并生成有证据的学习重点。Use when the user asks about AI jobs, target companies, compensation, interview experiences, interview questions, or market-driven study priorities.
---

# AI 岗位与面试情报流程

## 适用范围

用于研究用户明确指定公司或岗位的 AI/Agent 后端职位、岗位变化、薪资待遇、面试流程、高频题和学习重点。只做公开信息研究，不登录招聘账号，不自动投递，不绕过验证码；没有用户指定目标时先请求目标范围。provider catalog 只优化已明确的目标，不得被解释成内置关注公司名单。

## 执行顺序

1. 调用 `career_watchlist_show`。用户明确指定公司、关键词或城市时，以本轮要求为准；只有用户要求长期修改关注范围时才调用 `career_watchlist_update`。
2. 调用 `search_company_ai_jobs` 获取最近 30 天（用户另有要求时按用户窗口）的官方岗位和快照差异。
3. 检查每个 provider 的状态。对失败来源，把工具返回的 `fallback_query` 原样放入联网研究 task pack，不要声称已从官方接口抓到岗位。
4. 统一调用 `search_information`：公司官网、官方博客和通用网页使用 `source_hints=["web"]`；需要社区经验样本时加入 `experience`。同时需要岗位、薪资、待遇、面经和学习重点时，在一次请求中写明逻辑来源、各类必需字段、中国大陆、岗位 30 天/薪资面经 12 个月等时间窗口，由搜索入口规划并串行执行必要来源。
5. 对 fallback 来源，研究结果必须返回公司官方职位详情 URL、岗位名和可解析发布日期；主 Agent 审核后调用 `career_jobs_ingest`，并传入与本轮一致的关键词、城市、时间窗口和结果中的实际 `source_name`。搜索摘要、面经或社区 URL 不得写成岗位。
6. 让搜索 subagent 输出可被 `career_intel_ingest` 接收的 evidence records；每条包含 `source_type`。主 Agent 审核字段后再保存。搜索摘要或无正文信息只能使用 `source_type=search_snippet`、D 级。
7. 调用 `career_intel_query(since_days=365)` 合并本轮与历史证据，最后形成报告。需要完整 JD 时才传 `detail=true`。

## 来源与证据规则

- A：公司官方招聘页、官方技术博客或官方公告。
- B：包含发布日期、岗位方向和面试轮次的完整候选人经历。
- C：小红书、知乎、牛客、脉脉等个人经验，身份与内容无法完全核验。
- D：搜索摘要、转载、缺日期或缺关键上下文的信息，只能作为线索。
- 每条事实都保留公开 URL、来源日期、地区、岗位族和证据等级。链接打不开时明确说明。
- 薪资分别记录月薪、月数、奖金、股票和年包。缺少组成项时不得自行换算总包。
- 单一匿名样本不能代表公司普遍待遇。报告必须显示样本量和置信度。
- 面试题用稳定的 `normalized_key` 归一化。同一公司同一题至少有两个独立 URL 才称为“高频”；否则称为“个例”。
- `source_type` 只能为 `official` / `complete_experience` / `community_post` / `search_snippet` / `repost`；工具会根据域名自动降级来源等级，不能通过提示词提升级别。
- research fallback 快照不完整，只能说“本次未再次发现”；只有相同 scope 的完整 direct 扫描才可累计“疑似下线”。

## 报告格式

1. **岗位变化**：新增、变化、疑似下线、来源不可用；每项给官方或检索链接和发现时间。
2. **岗位要求**：按公司列出 Agent、后端、基础设施、业务要求和相关度依据。
3. **薪资待遇**：区分招聘标注、个人自述和推测；显示地区、级别、时间、样本数、置信度。
4. **面试流程与问题**：按公司和轮次汇总；区分高频与个例。
5. **学习重点**：
   - P0：多个目标岗位明确要求，且面经重复出现。
   - P1：岗位常见要求，但面经证据较少。
   - P2：公司或团队特有加分项。
   - 暂缓：缺少近期岗位证据。
6. **证据与限制**：列出来源、发布日期、冲突信息和仍无法确认的内容。

每个 P0/P1 学习项必须至少反向引用一个岗位和一个面经证据；证据不足时降级，不能补写通用课程充数。
