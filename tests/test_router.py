from local_agent_lab.config import load_config
from local_agent_lab.llm.model_router import route_task


def test_route_task_uses_configured_alias() -> None:
    config = load_config()
    route = route_task(config, task="code")
    assert route.profile.alias == "code_default"
    assert route.label == "LOCAL_OK"
