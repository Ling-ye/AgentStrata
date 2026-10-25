import {execFile} from 'node:child_process';
import {win32} from 'node:path';
import {Connection, Plugin, PluginId, localUrl} from './contracts';

export type Run = (args: string[], utf16?: boolean) => Promise<string>;
export const runWsl: Run = (args, utf16 = false) => new Promise((resolve, reject) => {
  // No shell expansion, command text or credentials supplied by web content.
  execFile(win32.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'wsl.exe'), args,
    {encoding: 'buffer', windowsHide: true, timeout: 180_000, maxBuffer: 2 * 1024 * 1024},
    (error, stdout) => {
      if (error) reject(new Error('WSL 命令失败或超时；检查发行版、命令路径及插件 doctor。'));
      else resolve(stdout.toString(utf16 ? 'utf16le' : 'utf8').replace(/^\uFEFF/, ''));
    });
});
export async function distributions(run: Run = runWsl): Promise<string[]> {
  return (await run(['--list', '--quiet'], true)).split(/\r?\n/).map(x => x.trim()).filter(Boolean);
}
export interface Backend {
  list(): Promise<Plugin[]>;
  start(id: PluginId): Promise<void>;
  stop(): Promise<void>;
  apply(id: string, version: number): Promise<{backup: string}>;
  restore(backup: string): Promise<void>;
}
export class WslBackend implements Backend {
  constructor(private readonly config: Connection, private readonly run: Run = runWsl) {}
  private async exec(command: string, args: string[]): Promise<any> {
    const raw = await this.run(['--distribution', this.config.distro, '--exec', command, ...args]);
    try { return JSON.parse(raw); } catch { throw new Error('WSL 返回了无效的插件结果'); }
  }
  private plugin(operation: string, id?: PluginId): Promise<any> {
    return this.exec(this.config.command, ['plugin', operation, ...(id ? [id] : []), '--root', this.config.root]);
  }
  async list(): Promise<Plugin[]> {
    const values = await this.plugin('list');
    if (!Array.isArray(values)) throw new Error('无法读取插件安装记录');
    return (['open-llm-vtuber', 'live2d-generator'] as PluginId[]).map(id => {
      const found = values.find(v => v.id === id);
      if (!found || found.status !== 'ready') throw new Error(`${id} 尚未安装就绪；请先在 WSL 安装插件。`);
      const expected = id === 'open-llm-vtuber' ? '0.4.0' : '0.3.0';
      if (found.installed_version !== expected) throw new Error(`${id} 需要配套版本 ${expected}`);
      return {...found, url: localUrl(found.url)};
    });
  }
  async start(id: PluginId): Promise<void> {
    const result = await this.plugin('start', id);
    if (result.ok !== true) throw new Error(`${id} 未通过启动就绪检查；请检查端口占用和插件 doctor。`);
  }
  async stop(): Promise<void> { await this.plugin('stop', 'open-llm-vtuber'); }
  private generator(args: string[]): Promise<any> {
    return this.exec(`${this.config.root}/live2d-generator/runtime/venv/bin/agentstrata-live2d`, args);
  }
  async apply(id: string, version: number): Promise<{backup: string}> {
    return this.generator(['characters', 'apply', '--root', `${this.config.root}/live2d-generator/data/jobs`,
      '--id', id, '--version', String(version), '--vtuber-root', `${this.config.root}/open-llm-vtuber`]);
  }
  async restore(backup: string): Promise<void> {
    await this.generator(['restore', '--vtuber-root', `${this.config.root}/open-llm-vtuber`, '--backup', backup]);
  }
}
