import {execFile, spawn, ChildProcessWithoutNullStreams} from 'node:child_process';
import {win32, join} from 'node:path';
import {Connection, Plugin, PluginId, localUrl} from './contracts';

export interface ModelSettings {
  revision: string; kind: 'openai_compatible' | 'chatgpt'; model: string; base_url: string; configured: boolean;
}
export interface Backend {
  list(): Promise<Plugin[]>;
  start(id: PluginId): Promise<void>;
  stop(): Promise<void>;
  apply(id: string, version: number): Promise<{backup: string}>;
  restore(backup: string): Promise<void>;
  perception(enabled: boolean, screenHash?: string): Promise<void>;
  prepare?(): Promise<void>;
  modelSettings?(value?: unknown): Promise<ModelSettings>;
  login?(): Promise<void>;
}
export type Run = (file: string, args: string[], input?: string) => Promise<string>;
export const runNative: Run = (file, args, input) => new Promise((resolve, reject) => {
  const child = execFile(file, args, {encoding: 'utf8', windowsHide: true,
    env: {...process.env, PYTHONUTF8: '1', PYTHONSAFEPATH: '1', PYTHONDONTWRITEBYTECODE: '1'}, timeout: 900_000, maxBuffer: 2 * 1024 * 1024},
    (error, stdout, stderr) => error ? reject(new Error((error as NodeJS.ErrnoException).code === 'ENOENT' ? '本机应用尚未准备，请在设置中准备运行环境。' : stderr.trim() || '本机后台命令失败，请检查应用日志。')) : resolve(stdout));
  child.stdin?.end(input);
});

export class NativeBackend implements Backend {
  private child?: ChildProcessWithoutNullStreams;
  constructor(private readonly config: Connection, private readonly payload: string,
    private readonly run: Run = runNative,
    private readonly spawnProcess: typeof spawn = spawn,
    private readonly trialHost?: string) {}
  private get python() {return win32.join(this.config.root, '.venv', 'Scripts', 'python.exe');}
  private async application(operation: string, input?: unknown): Promise<any> {
    const raw = await this.run(this.python, ['-m', 'chatcopilot', 'app', operation, '--root', this.config.root,
      ...(input === undefined ? [] : ['--save'])], input === undefined ? undefined : JSON.stringify(input));
    try {return JSON.parse(raw);} catch {throw new Error('本机应用返回了无效结果');}
  }
  async prepare(): Promise<void> {
    const powershell = win32.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    await this.run(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      this.trialHost ? join(this.trialHost, 'bootstrap-windows.ps1') :
        join(this.payload, 'source', 'integrations', 'live2d_desktop', 'scripts', 'bootstrap-windows.ps1'),
      '-BundleRoot', this.payload, '-InstallRoot', this.config.root,
      ...(this.trialHost ? ['-TrialProvisioner', join(this.trialHost, 'windows_setup.py')] : [])]);
  }
  async list(): Promise<Plugin[]> {
    const application = await this.application('status');
    if (typeof application.bot_id !== 'string' || !Array.isArray(application.plugins))
      throw new Error('无法读取本机应用安装记录');
    const client = application.plugins.find((value: any) => value.id === 'open-llm-vtuber');
    if (!client || client.status !== 'ready' || typeof client.installed_version !== 'string')
      throw new Error('Live2D 尚未准备完成，请在设置中准备本机应用。');
    return [{...client, url: localUrl(client.url)}];
  }
  async start(id: PluginId): Promise<void> {
    if (id !== 'open-llm-vtuber') throw new Error('角色生成器尚未包含在原生运行端。');
    return this.startApplication();
  }
  async startApplication(): Promise<void> {
    if (this.child && this.child.exitCode === null) {
      if ((await this.application('status')).ok === true) return;
      throw new Error('本机应用仍在启动或停止，请稍候。');
    }
    await new Promise<void>((resolve, reject) => {
      const child = this.spawnProcess(this.python, ['-m', 'chatcopilot', 'app', 'run', '--root', this.config.root, '--watch-stdin'],
        {windowsHide: true, env: {...process.env, PYTHONUTF8: '1', PYTHONSAFEPATH: '1', PYTHONDONTWRITEBYTECODE: '1',
          PYTHONPATH: win32.join(this.config.root, 'src')}});
      this.child = child;
      let output = '', errors = '', settled = false;
      const finish = (error?: Error) => {if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve();};
      const timer = setTimeout(() => {child.stdin.end(); finish(new Error('本机应用启动超时，请查看应用日志。'));}, 180_000);
      child.stderr.on('data', chunk => {errors = (errors + String(chunk)).slice(-4096);});
      child.stdout.on('data', chunk => {
        output += String(chunk);
        for (let boundary; (boundary = output.indexOf('\n')) >= 0;) {
          const line = output.slice(0, boundary); output = output.slice(boundary + 1);
          try {if (JSON.parse(line).ok === true) finish();} catch { /* A readiness result is required. */ }
        }
      });
      child.once('error', error => {if (this.child === child) this.child = undefined; finish(error);});
      child.once('exit', () => {if (this.child === child) this.child = undefined;
        finish(new Error(errors.trim() || '本机后台已退出，请检查模型设置及应用日志。'));});
    });
  }
  async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    if (child.exitCode === null) {
      // Closing our own lifeline cannot stop a supervisor started by another
      // desktop/CLI process for the same installation.
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('本机后台停止尚未确认，请检查应用日志。')), 40_000);
        child.once('exit', () => {clearTimeout(timer); resolve();});
        child.once('error', error => {clearTimeout(timer); reject(error);});
        child.stdin.end();
      });
    }
    this.child = undefined;
  }
  async modelSettings(value?: unknown): Promise<ModelSettings> {return this.application('model-settings', value);}
  async login(): Promise<void> {
    const powershell = win32.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    await this.run(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      join(this.payload, 'source', 'integrations', 'live2d_desktop', 'scripts', 'bootstrap-windows.ps1'),
      '-BundleRoot', this.payload, '-InstallRoot', this.config.root, '-AuthOnly']);
    if ((await this.application('login')).ok !== true) throw new Error('ChatGPT 登录未完成');
  }
  async apply(): Promise<{backup: string}> {throw new Error('角色生成器尚未包含在原生运行端。');}
  async restore(): Promise<void> {throw new Error('角色生成器尚未包含在原生运行端。');}
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
  async perception(enabled: boolean, screenHash = ''): Promise<void> {
    const info = await this.connectionInfo();
    const response = await fetch(`${info.url}/agentstrata/mcp/`, {method: 'POST', signal: AbortSignal.timeout(15_000),
      headers: {'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', Authorization: info.auth},
      body: JSON.stringify({jsonrpc: '2.0', id: 1, method: 'tools/call', params: {name: 'observe_desktop', arguments: {enabled, screen_hash: screenHash}}})});
    if (!response.ok) throw new Error('桌面感知连接不可用');
    const result = await response.json() as any;
    if (result.error || result.result?.isError) throw new Error('桌面感知请求未完成');
    if (result.result?.structuredContent?.state === 'unavailable') throw new Error('角色尚未连接，持续感知未启动');
  }
}
