"""Export previews from exactly the worker loader, without answers or grading code."""
import json
import sys
from agentrl.worker.config import ConfigLoader
from agentrl.worker.typings import InstanceFactory

name, revision = sys.argv[1:]
config = "configs/dbbench.yaml" if name == "dbbench-std" else "configs/os.yaml"
task = InstanceFactory.model_validate(ConfigLoader().load_from(config, name)[name]).create()
for index in task.get_indices():
    if name == "dbbench-std":
        prompt = task.dataset[index][0]["description"]
    else:
        prompt = task.problem_configs[index]["config"].description
    print(json.dumps({"task": name, "index": index, "input": prompt, "source_revision": revision}, ensure_ascii=False))
