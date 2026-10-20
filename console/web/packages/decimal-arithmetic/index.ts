// Copyright (C) 2026 Lingye
// SPDX-License-Identifier: AGPL-3.0-only
import Decimal from "decimal.js";

// Arco Slider/Rate consume these four operations. Two IEEE-754 operands need at
// most 34 significant decimal digits; keep guard digits in a private context.
const Arithmetic = Decimal.clone({ precision: 40 });
type Operand = number | string;
function calculate(method: "plus" | "minus" | "times" | "div", left: Operand, right: Operand, rest: Operand[]): number {
  return [right, ...rest].reduce((value, operand) => value[method](operand), new Arithmetic(left)).toNumber();
}
export const plus = (left: Operand, right: Operand, ...rest: Operand[]) => calculate("plus", left, right, rest);
export const minus = (left: Operand, right: Operand, ...rest: Operand[]) => calculate("minus", left, right, rest);
export const times = (left: Operand, right: Operand, ...rest: Operand[]) => calculate("times", left, right, rest);
export const divide = (left: Operand, right: Operand, ...rest: Operand[]) => calculate("div", left, right, rest);
// Arco disables the old binary-arithmetic warnings. This decimal backend has no
// such warning mode or shared mutable diagnostic configuration.
export function enableBoundaryChecking(_enabled: boolean): void {}
export default { plus, minus, times, divide, enableBoundaryChecking };
