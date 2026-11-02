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
  perception(enabled: boolean, screenHash?: string): Promise<void>;
}
export class WslBackend implements Backend {
  constructor(private readonly config: Connection, private readonly run: Run = runWsl) {}
  private get command() { return `${this.config.root}/.venv/bin/agentstrata`; }
  private get pluginsRoot() { return `${this.config.root}/.agentstrata-data/plugins`; }
  private async exec(command: string, args: string[]): Promise<any> {
    const raw = await this.run(['--distribution', this.config.distro, '--exec', command, ...args]);
    try { return JSON.parse(raw); } catch { throw new Error('WSL 返回了无效的插件结果'); }
  }
  private plugin(operation: string, id?: PluginId): Promise<any> {
    return this.exec(this.command, ['plugin', operation, ...(id ? [id] : []), '--root', this.pluginsRoot,
      '--application-root', this.config.root]);
  }
  private application(operation: string): Promise<any> {
    return this.exec(this.command, ['app', operation, '--root', this.config.root]);
  }
  async list(): Promise<Plugin[]> {
    const application = await this.application('status');
    if (!application || typeof application.bot_id !== 'string' || !Array.isArray(application.plugins))
      throw new Error('无法读取独立应用安装记录');
    const values = await this.plugin('list');
    if (!Array.isArray(values)) throw new Error('无法读取应用组件');
    const client = values.find(v => v.id === 'open-llm-vtuber');
    if (!client || client.status !== 'ready') throw new Error('Live2D 组件尚未安装就绪；请先完成 app setup。');
    const expected = application.plugins.find((v: any) => v.id === 'open-llm-vtuber');
    if (!expected || !client.installed_version || client.installed_version !== expected.installed_version)
      throw new Error('Live2D 组件版本与应用安装记录不一致');
    return values.filter(v => ['open-llm-vtuber', 'live2d-generator'].includes(v.id) && v.status === 'ready')
      .map(v => ({...v, url: localUrl(v.url)}));
  }
  async start(id: PluginId): Promise<void> {
    const result = id === 'open-llm-vtuber' ? await this.application('start') : await this.plugin('start', id);
    if (result.ok !== true) throw new Error(`${id} 未通过启动就绪检查；请检查端口占用和插件 doctor。`);
  }
  async stop(): Promise<void> { await this.application('stop'); }
  async perception(enabled: boolean, screenHash = ''): Promise<void> {
    const info = await this.connectionInfo();
    const response = await fetch(`${info.url}/agentstrata/mcp/`, {
      method: 'POST', signal: AbortSignal.timeout(15_000),
      headers: {'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', Authorization: info.auth},
      body: JSON.stringify({jsonrpc: '2.0', id: 1, method: 'tools/call',
        params: {name: 'observe_desktop', arguments: {enabled, screen_hash: screenHash}}}),
    });
    if (!response.ok) throw new Error('桌面感知连接不可用');
    const result = await response.json() as any;
    if (result.error || result.result?.isError) throw new Error('桌面感知请求未完成');
    if (result.result?.structuredContent?.state === 'unavailable') throw new Error('角色尚未连接，持续感知未启动');
  }
  private info?: {url: string; auth: string};
  private async connectionInfo() {
    if (!this.info) {
      const value = await this.application('desktop-info');
      if (typeof value.control_auth !== 'string' || !/^Bearer [A-Za-z0-9_-]{32,128}$/.test(value.control_auth))
        throw new Error('应用桌面连接凭据不可用');
      this.info = {url: localUrl(value.avatar_url), auth: value.control_auth};
    }
    return this.info;
  }
  private generator(args: string[]): Promise<any> {
    return this.exec(`${this.pluginsRoot}/live2d-generator/runtime/venv/bin/agentstrata-live2d`, args);
  }
  async apply(id: string, version: number): Promise<{backup: string}> {
    return this.generator(['characters', 'apply', '--root', `${this.pluginsRoot}/live2d-generator/data/jobs`,
      '--id', id, '--version', String(version), '--vtuber-root', `${this.pluginsRoot}/open-llm-vtuber`, '--presentation-only']);
  }
  async restore(backup: string): Promise<void> {
    await this.generator(['restore', '--vtuber-root', `${this.pluginsRoot}/open-llm-vtuber`, '--backup', backup]);
  }
}
