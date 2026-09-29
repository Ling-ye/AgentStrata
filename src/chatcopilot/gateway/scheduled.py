"""Adapter from schedule execution ports to the existing Gateway coordinator."""
from dataclasses import asdict

from chatcopilot.gateway.observations import response_outbound_id


class GatewayScheduleExecutor:
    def __init__(self, coordinator, state_store, account):
        self.coordinator, self.state_store, self.account = coordinator, state_store, account

    async def execute(self, run, *, on_generated):
        return await self.coordinator.execute_scheduled(run_id=run["gateway_run_id"],
            schedule_id=run["task_id"], account=self.account, group_id=run["settings"]["group_id"],
            prompt=run["prompt"], preview=run["preview"], on_generated=on_generated)

    def recover(self, run):
        receipts = self.state_store.delivery_receipts(response_outbound_id(run["gateway_run_id"]))
        if any(receipt.stage == "provider_acknowledged" for receipt in receipts):
            return {"status": "delivered", "receipts": [asdict(item) for item in receipts], "error_code": ""}
        if receipts:
            last = receipts[-1]
            return {"status": "failed" if last.stage in {"failed", "gateway_accepted"} else "delivery_unknown",
                    "receipts": [asdict(item) for item in receipts], "error_code": last.error_code or "delivery_interrupted"}
        if run["preview"] and run["generated_at"] and not run["cancel_requested"]:
            return {"status": "previewed", "error_code": ""}
        return {"status": "interrupted", "error_code": "host_interrupted"}
