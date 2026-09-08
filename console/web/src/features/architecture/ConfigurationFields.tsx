import { ConfigFields, Disclosure, FIELD_NAMES, TextPreview } from "./ObservationContent";
import type { InspectionEntity } from "./workbenchModel";

const ENV_NAMES: Record<string, string> = {
  QQ_ALLOW_FROM: "用户白名单", QQ_ALLOW_GROUPS: "群白名单", CHATCOPILOT_OWNERS: "Owner", CHATCOPILOT_ADMINS: "Admin",
  QQ_ACCOUNT: "机器人账号", QQ_ACCESS_TOKEN: "OneBot 认证令牌", CHATCOPILOT_GATEWAY_PORT: "监听端口",
  CHATCOPILOT_GATEWAY_STATE_ROOT: "状态存储目录", CHATCOPILOT_GATEWAY_TOKEN: "Gateway 认证令牌",
};
function envLabel(key: string) {
  return ENV_NAMES[key] ?? key.replace(/^(CHATCOPILOT_|QQ_)/, "").replace(/_/g, " ").toLowerCase()
    .replace(/api key$/, "API 凭据").replace(/base url$/, "服务地址").replace(/model$/, "模型").replace(/timeout$/, "超时（秒）");
}
export default function ConfigurationFields({ entity }: { entity: InspectionEntity }) {
  const values = entity.environment ?? {};
  const used = new Set<string>();
  const resolve = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.length && value.every((item) => item == null || typeof item !== "object")
      ? value.map((item) => item == null ? "未配置" : String(item)).join("、") : value.map(resolve);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      if (key.endsWith("_env") && typeof item === "string" && item) {
        used.add(item);
        return [ENV_NAMES[item] ?? FIELD_NAMES[key.slice(0, -4)] ?? FIELD_NAMES[key] ?? key.slice(0, -4), values[item] ?? null];
      }
      return [ENV_NAMES[key] ?? key, resolve(item)];
    }));
    if (typeof value === "string") return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g, (match, key: string, fallback?: string) => {
      used.add(key); return values[key] == null || values[key] === "" ? fallback ?? (values[key] === "" ? "" : match) : String(values[key]);
    });
    return value;
  };
  const config = { ...entity.config };
  const parameters = config.parameters ?? config.input_schema;
  delete config.parameters; delete config.input_schema;
  const fields = resolve(config);
  const remainder = Object.fromEntries(Object.entries(values).filter(([key]) => !used.has(key)).map(([key, value]) => [envLabel(key), value]));
  const schema = parameters && typeof parameters === "object" ? parameters as Record<string, unknown> : null;
  const properties = schema?.properties && typeof schema.properties === "object" ? schema.properties as Record<string, Record<string, unknown>> : null;
  return <>
    <ConfigFields value={fields} missingLabel="未配置" />
    {!!Object.keys(remainder).length && <ConfigFields value={remainder} missingLabel="未配置" />}
    {parameters != null && <Disclosure title="工具参数">{properties ? <div className="obs-parameter-table"><table className="obs-table"><thead><tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr></thead>
      <tbody>{Object.entries(properties).map(([name, field]) => <tr key={name}><td>{name}</td><td>{String(field.type ?? "未记录")}</td>
        <td>{Array.isArray(schema?.required) && schema.required.includes(name) ? "是" : "否"}</td><td>{String(field.description ?? "—")}</td></tr>)}</tbody></table>
      <Disclosure title="原始参数定义"><TextPreview text={JSON.stringify(parameters, null, 2)} /></Disclosure></div> : <ConfigFields value={parameters} />}</Disclosure>}
    <Disclosure title="配置来源"><ConfigFields value={entity.source_config ?? entity.config} missingLabel="未配置" />
      <ConfigFields value={entity.source_environment ?? entity.environment} missingLabel="未配置" /></Disclosure>
  </>;
}
