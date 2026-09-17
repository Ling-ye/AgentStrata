"""Assemble the optional Harness control surface once, before serving requests."""
from chatcopilot.harness.api import HarnessController
from console.control.discovery import repo_root


def create_controller() -> HarnessController:
    from chatcopilot.harness.gateway_adapter import task_source
    from chatcopilot.harness.models import HarnessError
    from console.backend.routes.common import get_instance
    from console.control.gateway_observability import reader

    def task_reader(bot_id, run_id):
        instance = get_instance(bot_id)
        if instance.runtime_kind != "gateway":
            raise HarnessError("unsupported_source", "此实例不提供 Gateway 任务观测")
        return task_source(reader(instance), instance.instance_id, run_id)

    from console.control.gateway_observability import retained_task_images
    return HarnessController(repo_root(), task_reader=task_reader,
                             image_reader=lambda bot_id, run_id: retained_task_images(get_instance(bot_id), run_id))

