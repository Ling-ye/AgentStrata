import {ApplyInput, Plugin, Result, identifier} from './contracts';
import {Backend} from './wsl';

export interface Renderer {
  prepare(): Promise<boolean>;
  resume(reload: boolean): Promise<void>;
}
export class Controller {
  private busy = false;
  constructor(private readonly backend: Backend, private readonly renderer: Renderer,
    private readonly reachable: (plugin: Plugin) => Promise<void>) {}

  async connect(): Promise<Plugin[]> {
    if (this.busy) throw new Error('后台操作正在进行');
    this.busy = true;
    try {
      const plugins = await this.backend.list();
      // systemd start is idempotent and doctor checks the actual service, not just an open port.
      for (const plugin of plugins) {
        await this.backend.start(plugin.id);
        await this.reachable(plugin);
      }
      return plugins;
    } finally { this.busy = false; }
  }
  async change(input: ApplyInput | {backup: string}): Promise<Result> {
    if (this.busy) return {ok: false, message: '另一个后台操作正在进行'};
    this.busy = true;
    let prepared = true, stopped = false, backup: string | undefined;
    let changed = false;
    try {
      const idle = await this.renderer.prepare();
      if (!idle) return {ok: false, message: '请先停止当前对话和语音，再切换角色'};
      await this.backend.stop();
      stopped = true;
      if ('id' in input) backup = identifier((await this.backend.apply(input.id, input.version)).backup);
      else { backup = input.backup; await this.backend.restore(backup); }
      changed = true;
      await this.backend.start('open-llm-vtuber');
      const client = (await this.backend.list()).find(p => p.id === 'open-llm-vtuber')!;
      await this.reachable(client);
      await this.renderer.resume(true);
      prepared = false;
      return 'id' in input ? {ok: true, backup} : {ok: true, restored: backup};
    } catch {
      if (!stopped) return {ok: false, message: '无法停止交互后台，角色未应用'};
      try {
        if (changed) {
          await this.backend.stop();
          // Restore already has its own atomic transaction. A failed restart after an explicit
          // restore needs operator attention; applying the same receipt twice is not an undo.
          if (!('id' in input)) throw new Error('Restored service did not start');
          await this.backend.restore(backup!);
        }
        await this.backend.start('open-llm-vtuber');
        const client = (await this.backend.list()).find(p => p.id === 'open-llm-vtuber')!;
        await this.reachable(client);
        await this.renderer.resume(true);
        prepared = false;
        return {ok: false, recovered: changed, backup, message: changed ? '角色操作失败；原配置已恢复并重新启动' :
          '应用未返回成功结果；交互后台已重新启动，请在角色详情核对当前配置'};
      } catch {
        return {ok: false, recovered: false, backup,
          message: `后台恢复失败，请检查 WSL 插件 doctor。保留备份：${backup ?? '应用事务未返回备份 ID'}`};
      }
    } finally {
      if (prepared) await this.renderer.resume(false).catch(() => undefined);
      this.busy = false;
    }
  }
}
