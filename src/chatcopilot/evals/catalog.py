"""Benchmark catalog metadata.

Only benchmarks that have adapters or planned adapters are registered.
"""

from __future__ import annotations

from chatcopilot.evals.models import BenchmarkStandard


STANDARDS: tuple[BenchmarkStandard, ...] = (
    BenchmarkStandard(
        suite_id="gaia",
        name="GAIA",
        kind="agent",
        value="真实世界多步任务，常需搜索、工具和综合推理，是 Agent 综合能力的标准标尺。",
        recommendation="Agent 机制或工具改动后运行；先用 Level 1 小样本，再跑 balanced-100 固定集。",
        cadence="weekly/release",
        requires_external_data=True,
        official_url="https://huggingface.co/datasets/gaia-benchmark/GAIA",
        setup_hint=(
            "设置 CHATCOPILOT_GAIA_DATA_PATH 指向官方 JSON/JSONL，"
            "可选 CHATCOPILOT_GAIA_FILES_DIR/LEVELS/MAX_CASES/CASE_PROFILE；"
            "先用 Level 1 小样本固定 search_information/Tavily 工具环境后再比较分数。"
        ),
    ),
    BenchmarkStandard(
        suite_id="bfcl",
        name="BFCL",
        kind="tool",
        value="函数调用与参数构造评测，直接对应工具选择、schema 遵循和调用准确性。",
        recommendation="改工具注册、selector、payload filter 或 tool prompt 后运行；外部数据默认 balanced-100。",
        cadence="weekly/regression",
        official_url="https://gorilla.cs.berkeley.edu/leaderboard.html",
        setup_hint=(
            "内置 smoke 子集可直接运行；完整评测需下载 "
            "gorilla-llm/Berkeley-Function-Calling-Leaderboard 并设置 "
            "CHATCOPILOT_BFCL_DATA_DIR 指向数据目录；默认按调用复杂度映射 Level 并选 100 题。"
        ),
    ),
    BenchmarkStandard(
        suite_id="ifeval",
        name="IFEval",
        kind="safety",
        value="指令遵循评测，检测格式、约束、角色和输出要求是否被正确执行。",
        recommendation="每次改系统 prompt 后运行；外部数据默认 balanced-100，未配置时使用内置 smoke 子集。",
        cadence="daily/regression",
        official_url="https://github.com/google-research/google-research/tree/master/instruction_following_eval",
        setup_hint="可选：设置 CHATCOPILOT_IFEVAL_DATA_PATH 指向官方 input_data.jsonl；默认按指令复杂度映射 Level 并选 100 题。",
    ),
    BenchmarkStandard(
        suite_id="swe-bench-verified",
        name="SWE-bench Verified",
        kind="code",
        value="真实 GitHub issue 修复，最接近工程 Agent 端到端代码修改能力。",
        recommendation="仅在代码 Agent 机制大改后低频运行。",
        cadence="major-change",
        requires_external_data=True,
        official_url="https://www.swebench.com/",
        setup_hint=(
            "需要 Docker 沙箱基础设施：clone repo → worktree → Agent 修改 → "
            "Docker 内运行测试套件。设置 CHATCOPILOT_SWEBENCH_DATA_PATH 指向官方 JSONL。"
        ),
    ),
    BenchmarkStandard(
        suite_id="webarena",
        name="WebArena",
        kind="web",
        value="浏览器/Web 环境操作任务，衡量网页理解、点击、表单和多步操作能力。",
        recommendation="需要 Agent 具备浏览器工具能力后再运行。",
        cadence="major-change",
        requires_external_data=True,
        official_url="https://webarena.dev/",
        setup_hint=(
            "需要 Agent 浏览器操作工具 + 自建 WebArena Web 应用集群（Reddit/GitLab/Shopping Docker）。"
            "当前 Agent 无浏览器能力，此 benchmark 暂不可用。"
        ),
    ),
)
