from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from local_agent_lab.cli import app
from local_agent_lab.config import load_config
from local_agent_lab.llm.model_router import route_task


runtime_home = Path(os.environ["LAGENT_HOME"]).resolve()
config = load_config()
route = route_task(config, task="code")

assert config.root_dir == runtime_home
assert config.path.name == "agent.yaml"
assert "site-packages" not in str(config.logs_dir)
assert config.prompt_path("chat").is_file()
assert config.prompt_path("review").is_file()
assert route.profile.alias == "code_default"
health = CliRunner().invoke(app, ["health"])
assert health.exit_code == 0, health.output
print(f"wheel-smoke ok python-root={runtime_home} config={config.path}")
