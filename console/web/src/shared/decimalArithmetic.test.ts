import { describe, expect, it } from "vitest";
import arithmetic, { plus, minus, times, divide } from "../../packages/decimal-arithmetic";
import Decimal from "decimal.js";

describe("Arco decimal arithmetic", () => {
  it("keeps slider steps and offsets free of binary rounding artifacts", () => {
    expect(plus(0.1, 0.2)).toBe(0.3);
    expect(minus(0.3, 0.1)).toBe(0.2);
    expect(times(0.7, 100)).toBe(70);
    expect(divide(0.3, 0.1)).toBe(3);
    expect(divide(minus(0.2, 0.1), minus(0.3, 0.1))).toBe(0.5);
  });
  it("supports fractional ratings, negative ranges and string operands", () => {
    expect(arithmetic.times(+arithmetic.divide(3.7, 0.5).toFixed(0), 0.5)).toBe(3.5);
    expect(plus(-0.2, 0.1)).toBe(-0.1);
    expect(plus("0.1", "0.2", "0.3")).toBe(0.6);
    expect(times(0.01, 0.01)).toBe(0.0001);
  });
  it("does not change another consumer's decimal configuration", () => {
    const precision = Decimal.precision;
    arithmetic.enableBoundaryChecking(false);
    plus(1, 2);
    expect(Decimal.precision).toBe(precision);
  });
});
