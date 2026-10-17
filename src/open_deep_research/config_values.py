# 提供枚举、空值和普通配置项的统一取值入口。
"""Read scalar or enum configuration values."""

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
