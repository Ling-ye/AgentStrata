import { describe, expect, it } from "vitest";
import { activeRun, defaultSettings, displayTime, scheduleLabel, settingsError, tweetTemplate, type ScheduleRun } from "./model";

describe("scheduled task configuration", () => {
  it("defaults to paused and gives clear missing-target errors", () => {
    const value = defaultSettings();
    expect(value.enabled).toBe(false);
    expect(settingsError(value)).toContain("名称");
    Object.assign(value, { name: "昨日推文", instruction: tweetTemplate });
    expect(settingsError(value)).toContain("群号");
    value.group_id = "30003";
    expect(settingsError(value)).toBe("");
    value.timezone = "invalid";
    expect(settingsError(value)).toContain("时区");
  });
  it("renders daily/weekly and task timezone consistently", () => {
    expect(scheduleLabel(defaultSettings())).toBe("每天 09:00 · Asia/Shanghai");
    expect(scheduleLabel({ ...defaultSettings(), weekdays: [0, 4] })).toBe("周一、周五 09:00 · Asia/Shanghai");
    expect(displayTime(Date.parse("2026-09-28T01:00:00Z") / 1000)).toContain("09:00:00");
    expect(displayTime(null)).toBe("—");
  });
  it("does not poll terminal or uncertain deliveries as active work", () => {
    for (const status of ["queued", "researching", "delivering"]) expect(activeRun({ status } as ScheduleRun)).toBe(true);
    for (const status of ["previewed", "failed", "delivery_unknown", "delivered", "cancelled", "skipped"]) expect(activeRun({ status } as ScheduleRun)).toBe(false);
    expect(activeRun()).toBe(false);
  });
});
