# AgentStrata Common Deploy

Common deployment owns instance-neutral concepts:

- selected BotSpec: `CHATCOPILOT_BOT_SPEC`
- instance id: `CHATCOPILOT_INSTANCE_ID`
- workspace root: `CHATCOPILOT_WORKSPACE_ROOT`
- log dir: `CHATCOPILOT_LOG_DIR`

Platform-specific launchers should eventually call:

```bash
python -m chatcopilot run --bot "$CHATCOPILOT_BOT_SPEC"
```
