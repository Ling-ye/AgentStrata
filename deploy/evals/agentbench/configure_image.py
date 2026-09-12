"""Apply the declared DB resource profile inside the disposable worker image."""
from pathlib import Path
import agentrl.worker.environment.docker as docker_environment

path = Path("src/server/tasks/dbbench/environment.py")
source = path.read_text()
replacements = {
    "'--max_connections=2000'": "'--max_connections=32'",
    "'--thread_cache_size=512'": "'--thread_cache_size=8'",
    "'--innodb_buffer_pool_size=32G'": "'--innodb_buffer_pool_size=128M'",
    "'--innodb_buffer_pool_instances=4'": "'--innodb_buffer_pool_instances=1'",
    "'--innodb_log_file_size=1G'": "'--innodb_log_file_size=64M'",
    "'--innodb_log_buffer_size=64M'": "'--innodb_log_buffer_size=16M'",
    "'--table_open_cache=10000'": "'--table_open_cache=256'",
    "'--table_definition_cache=4096'": "'--table_definition_cache=256'",
    "'--performance_schema=ON'": "'--performance_schema=OFF'",
    "return 64": "return 1",
    "        return attrs": "        attrs['HostConfig'].update(Memory=1073741824, NanoCpus=1000000000, PidsLimit=256)\n        return attrs",
}
for before, after in replacements.items():
    if source.count(before) != 1:
        raise ValueError("pinned DB resource configuration differs")
    source = source.replace(before, after)
path.write_text(source)

# AgentRL passes an integer to aiodocker's stream API, which requires the
# aiohttp timeout DTO. Preserve the same timeout while adapting that API.
path = Path(docker_environment.__file__)
source = path.read_text()
before = "async with exec_.start(timeout=timeout, detach=False) as stream:"
after = "async with exec_.start(timeout=aiohttp.ClientTimeout(total=timeout), detach=False) as stream:"
if source.count(before) != 1:
    raise ValueError("pinned AgentRL exec timeout call differs")
path.write_text(source.replace(before, after))
