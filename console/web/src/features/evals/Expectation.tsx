import { Typography } from "@arco-design/web-react";
import type { CaseExpectation } from "./model";

const { Text } = Typography;
const answerText = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

export function expectationText(value?: CaseExpectation): string {
  if (!value) return "预期未记录";
  if (value.reference_answer !== null && value.reference_answer !== undefined) return answerText(value.reference_answer);
  return value.behavior || value.checks.join("；") || "本题按行为／状态校验";
}

export function Expectation({ value }: { value?: CaseExpectation }) {
  if (!value) return <Text type="secondary">预期未记录</Text>;
  const hasAnswer = value.reference_answer !== null && value.reference_answer !== undefined;
  return <div className="eval-expectation" aria-label="预期回答与校验要求">
    {hasAnswer && <section><Text bold>参考答案</Text><pre>{answerText(value.reference_answer)}</pre></section>}
    {value.behavior && <section><Text bold>预期行为</Text><pre>{value.behavior}</pre></section>}
    {!!value.checks.length && <section><Text bold>必须满足的校验要求</Text><ul>{value.checks.map((check, index) => <li key={index}>{check}</li>)}</ul></section>}
    {!hasAnswer && <Text type="secondary">本题按行为／状态校验，不要求唯一回答话术。</Text>}
  </div>;
}
