import {win32} from 'node:path';
export type Mode = 'window' | 'pet';
export type PluginId = 'open-llm-vtuber' | 'live2d-generator';
export interface Connection { root: string }
export interface Bounds { x: number; y: number; width: number; height: number }
export interface Settings { connection?: Connection; mode: Mode; topmost: boolean; bounds?: Bounds }
export function launchConnection(args: string[], saved?: Settings): Connection | undefined {
  const value = (name: string) => {
    const index = args.indexOf(name);
    return index < 0 ? undefined : args[index + 1];
  };
  if (args.includes('--application-root'))
    return connection({root: value('--application-root')});
  return saved?.connection ? connection(saved.connection) : undefined;
}
export interface Plugin { id: PluginId; status: string; url: string; installed_version: string | null }
export interface ApplyInput { id: string; version: number }
export type Result = { ok: true; backup?: string; restored?: string } |
  { ok: false; message: string; backup?: string; recovered?: boolean };

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('参数格式错误');
  return value as Record<string, unknown>;
}
export function connection(value: unknown): Connection {
  const v = record(value);
  for (const key of ['root']) {
    if (typeof v[key] !== 'string' || !/^[A-Za-z]:[\\/]/.test(v[key] as string) ||
        /[\x00-\x1f]/.test(v[key] as string) || (v[key] as string).length > 2048)
      throw new Error('应用安装目录必须是 Windows 本地磁盘的绝对路径');
  }
  const root = win32.normalize(v.root as string);
  if (root === win32.parse(root).root) throw new Error('请为应用选择独立目录');
  return {root: root.replace(/[\\/]+$/, '')};
}
export function identifier(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value)) throw new Error('角色或备份 ID 无效');
  return value;
}
export function applyInput(value: unknown): ApplyInput {
  const v = record(value);
  if (!Number.isSafeInteger(v.version) || (v.version as number) < 1) throw new Error('角色版本无效');
  return {id: identifier(v.id), version: v.version as number};
}
export function localUrl(value: unknown): string {
  if (typeof value !== 'string') throw new Error('安装记录缺少服务地址');
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    throw new Error('服务地址必须是本机 HTTP 根地址');
  return url.origin;
}
export function settings(value: unknown): Settings {
  const v = record(value);
  const result: Settings = {mode: v.mode === 'window' ? 'window' : 'pet', topmost: v.topmost === true};
  if (v.connection) result.connection = connection(v.connection);
  if (v.bounds) {
    const b = record(v.bounds);
    if (['x', 'y', 'width', 'height'].every(k => Number.isSafeInteger(b[k])) &&
        (b.width as number) >= 640 && (b.height as number) >= 480)
      result.bounds = {x: b.x as number, y: b.y as number, width: b.width as number, height: b.height as number};
  }
  return result;
}
export function visibleBounds(saved: Bounds | undefined, area: Bounds): Bounds {
  const width = Math.min(saved?.width ?? 1000, area.width);
  const height = Math.min(saved?.height ?? 760, area.height);
  return {width, height, x: Math.max(area.x, Math.min(saved?.x ?? area.x + 40, area.x + area.width - width)),
    y: Math.max(area.y, Math.min(saved?.y ?? area.y + 40, area.y + area.height - height))};
}
