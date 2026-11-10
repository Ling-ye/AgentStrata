"""Read-only source eligibility, not an execution or installation authorization."""
from pathlib import Path

from chatcopilot.evals.application.bots import EvaluationBotResolver, bot_spec_path


def improvement_readiness(applications, ident):
    row = applications.require(ident)
    checks, requirements = [], []
    supported = False
    scoped_prompt_supported = False
    if row['source'] == 'repository':
        try:
            selected = EvaluationBotResolver(applications.root)(row['instance_id'])
            resolved = bot_spec_path(selected, applications.root)
            discovered = [item for item in applications._discover()
                          if item.instance_id == row['instance_id'] and not item.read_only]
            if len(discovered) != 1 or resolved != Path(discovered[0].bot_spec).resolve():
                raise ValueError('ambiguous source')
            supported = True
            checks.append({'code': 'registered_repository_source', 'status': 'passed',
                           'message': '应用与现有测评解析器的源码身份一致。'})
        except (KeyError, ValueError, OSError, RuntimeError):
            checks.append({'code': 'source_registration_unavailable', 'status': 'blocked',
                           'message': '无法核实当前应用与已登记源码的对应关系。'})
    elif row['source'] == 'studio' and row['type'] == 'qq_official':
        # A Studio configuration directory is not a delivered installation. Even an added
        # installation record cannot register it with the repository-owned service.
        checks.extend([
            {'code': 'external_definition_not_registered', 'status': 'blocked',
             'message': '此 QQ 应用的运行配置位于 Studio 私有目录，尚未登记到测评服务的源码树。'},
            {'code': 'application_source_baseline_missing', 'status': 'blocked',
             'message': '尚未准备此应用的独立源码基线；可使用下方提示词改进流程进行显式准备。'},
            {'code': 'running_source_evidence_missing', 'status': 'blocked',
             'message': '尚无目标应用实际加载版本的来源摘要，不能据此发起 Harness 源码优化。'},
        ])
        source = applications.manager().source_status(row["project_id"])
        if source["source_bound"]:
            scoped_prompt_supported = True
            checks = [
                {"code": "application_scoped_source", "status": "passed",
                 "message": "已准备应用专属源码；请使用本应用的提示词改进入口。"},
                {"code": "global_services_not_bound", "status": "blocked",
                 "message": "仓库级 Eval/Harness 仍不属于此应用，不能复用全局入口。"},
                {"code": "running_source_evidence_missing", "status": "blocked",
                 "message": "独立接受、源码安装、进程加载与真实 QQ 往返仍是不同证据。"},
            ]
        requirements.extend([
            {'code': 'verified_application_delivery',
             'message': '显式准备此应用的完整公开源码，保持 BotSpec、Agent profile 和源码的相对路径。'},
            {'code': 'owned_installation_baseline',
             'message': '通过应用专属 Harness 独立验收，再由正式交付安装流程建立版本身份。'},
            {'code': 'application_scoped_services',
             'message': '只使用当前应用的专属开发入口；不将私有源码交给仓库级全局服务。'},
            {'code': 'current_runtime_evidence',
             'message': '在目标版本复测并记录实际加载的源码摘要，然后才可使用故障反馈和候选验收。'},
        ])
        if source["source_bound"]:
            requirements = requirements[1:] if source["state"] == "prepared" else requirements[-1:]
    else:
        checks.append({'code': 'source_adapter_not_supported', 'status': 'blocked',
                       'message': '此来源尚未接入统一测评与 Harness 源码管理，不能仅凭同名实例或外部路径复用结果。'})
    checks.append({'code': 'execution_preflight_not_run', 'status': 'not_checked',
                   'message': '本检查未调用模型、启动测评或开发任务；预算、模型、依赖及实际效果仍须单独核验。'})
    return {'application_id': ident, 'scope': 'source_registration', 'source_supported': supported,
            'execution_verified': False, 'scoped_prompt_supported': scoped_prompt_supported,
            'checks': checks, 'requirements': requirements}
