# Harness 编译图

由 `.venv/bin/python scripts/render_harness_graphs.py` 从实际 StateGraph 生成；使用 `--check` 检查是否与源码一致。这里只展示拓扑，不执行节点。状态、权限与恢复规则见 [Harness](../../docs/reference/harness.md)。

## 任务

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	environment(environment)
	prepare(prepare)
	reproduce(reproduce)
	begin_attempt(begin_attempt)
	main(main)
	plan(plan)
	bind(bind)
	coding(coding)
	test(test)
	assemble(assemble)
	failure(failure)
	acceptance(acceptance)
	choose(choose)
	delivery(delivery)
	__end__([<p>__end__</p>]):::last
	__start__ --> environment;
	acceptance -.-> __end__;
	acceptance -.-> delivery;
	acceptance -.-> failure;
	assemble -.-> __end__;
	assemble -.-> acceptance;
	assemble -.-> failure;
	begin_attempt -.-> __end__;
	begin_attempt -.-> acceptance;
	begin_attempt -.-> bind;
	begin_attempt -.-> failure;
	begin_attempt -.-> main;
	begin_attempt -.-> plan;
	bind -.-> __end__;
	bind -.-> choose;
	bind -.-> failure;
	choose -.-> __end__;
	choose -.-> assemble;
	choose -.-> coding;
	choose -.-> failure;
	choose -.-> test;
	coding -.-> __end__;
	coding -.-> choose;
	coding -.-> failure;
	environment -.-> __end__;
	environment -.-> failure;
	environment -.-> prepare;
	failure -.-> __end__;
	failure -.-> begin_attempt;
	main -.-> __end__;
	main -.-> bind;
	main -.-> failure;
	main -.-> plan;
	plan -.-> __end__;
	plan -.-> bind;
	plan -.-> failure;
	prepare -.-> __end__;
	prepare -.-> failure;
	prepare -.-> reproduce;
	reproduce -.-> __end__;
	reproduce -.-> begin_attempt;
	reproduce -.-> failure;
	test -.-> __end__;
	test -.-> choose;
	test -.-> failure;
	delivery --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 共用验收

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	definition(definition)
	baseline(baseline)
	verification(verification)
	regressions(regressions)
	confirmation(confirmation)
	review(review)
	seal(seal)
	__end__([<p>__end__</p>]):::last
	__start__ --> definition;
	baseline -. &nbsp;done&nbsp; .-> __end__;
	baseline -. &nbsp;next&nbsp; .-> verification;
	confirmation -. &nbsp;done&nbsp; .-> __end__;
	confirmation -. &nbsp;next&nbsp; .-> review;
	definition -. &nbsp;done&nbsp; .-> __end__;
	definition -. &nbsp;next&nbsp; .-> baseline;
	regressions -. &nbsp;done&nbsp; .-> __end__;
	regressions -. &nbsp;next&nbsp; .-> confirmation;
	review -. &nbsp;done&nbsp; .-> __end__;
	review -. &nbsp;next&nbsp; .-> seal;
	verification -. &nbsp;done&nbsp; .-> __end__;
	verification -. &nbsp;next&nbsp; .-> regressions;
	seal --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 交付

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	inspect(inspect)
	commit(commit)
	locate_pr(locate_pr)
	checks(checks)
	push(push)
	create_pr(create_pr)
	observe(observe)
	prepare_update(prepare_update)
	revalidate(revalidate)
	commit_update(commit_update)
	cleanup_update(cleanup_update)
	enable(enable)
	retire(retire)
	cancel(cancel)
	cleanup_wait(cleanup_wait)
	cleanup_done(cleanup_done)
	wait(wait)
	__end__([<p>__end__</p>]):::last
	__start__ --> inspect;
	cancel -.-> cleanup_done;
	cancel -.-> retire;
	checks -.-> cancel;
	checks -.-> push;
	cleanup_done -.-> __end__;
	cleanup_update -.-> cancel;
	cleanup_update -.-> observe;
	cleanup_wait -.-> cancel;
	cleanup_wait -.-> wait;
	commit -.-> cancel;
	commit -.-> locate_pr;
	commit_update -.-> cancel;
	commit_update -.-> checks;
	create_pr -.-> cancel;
	create_pr -.-> observe;
	enable -.-> cancel;
	enable -.-> cleanup_wait;
	enable -.-> observe;
	inspect -.-> cancel;
	inspect -.-> cleanup_done;
	inspect -.-> commit;
	inspect -.-> locate_pr;
	locate_pr -.-> cancel;
	locate_pr -.-> checks;
	locate_pr -.-> cleanup_done;
	locate_pr -.-> observe;
	observe -.-> cancel;
	observe -.-> cleanup_done;
	observe -.-> cleanup_wait;
	observe -.-> enable;
	observe -.-> prepare_update;
	observe -.-> retire;
	prepare_update -.-> cancel;
	prepare_update -.-> revalidate;
	push -.-> cancel;
	push -.-> cleanup_update;
	push -.-> create_pr;
	push -.-> observe;
	retire -.-> cancel;
	retire -.-> cleanup_done;
	revalidate -.-> cancel;
	revalidate -.-> commit_update;
	wait --> observe;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 代码熵回收批次

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	observe(observe)
	cancel(cancel)
	settle(settle)
	learning(learning)
	limits(limits)
	create(create)
	launch(launch)
	wait(wait)
	__end__([<p>__end__</p>]):::last
	__start__ --> observe;
	cancel -.-> __end__;
	cancel -.-> wait;
	create -.-> __end__;
	create -.-> launch;
	create -.-> wait;
	launch -.-> __end__;
	launch -.-> wait;
	learning -.-> __end__;
	learning -.-> create;
	learning -.-> limits;
	learning -.-> wait;
	limits -.-> __end__;
	limits -.-> create;
	limits -.-> wait;
	observe -.-> __end__;
	observe -.-> cancel;
	observe -.-> launch;
	observe -.-> learning;
	observe -.-> limits;
	observe -.-> settle;
	observe -.-> wait;
	settle -.-> __end__;
	settle -.-> limits;
	settle -.-> wait;
	wait -.-> observe;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
