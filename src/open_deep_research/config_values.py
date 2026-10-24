# 提供枚举、空值和普通配置项的统一取值入口。
"""Read scalar or enum configuration values."""


def without_retired_execution_selectors(values):
    """Accept former SDK/DBOS aliases, rejecting requests for retired engines.

    These input-only aliases are discarded. They are never execution switches.
    """
    result = dict(values)
    for name, allowed in (
        ("workflow_backend", {"python", "dbos"}),
        ("business_agent_runtime", {"openai_agents"}),
    ):
        if name in result:
            value = result.pop(name)
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(
                    f"{name} selects a retired or unsupported engine; use the DBOS Run API with SDK roles."
                )
    return result


# 提取枚举的实际值，普通值原样返回，并兼容 None。
def get_config_value(value):
    """Extract value from configuration, handling enums and None values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    elif isinstance(value, dict):
        return value
    else:
        return value.value
