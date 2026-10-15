# 提供提示词和报告中使用的日期格式化函数。
"""Date formatting shared by prompts and research tools."""

from datetime import datetime


# 返回当前日期的可读字符串，用于提示词和输出展示。
def get_today_str() -> str:
    """Get current date formatted for display in prompts and outputs.

    Returns:
        Human-readable date string in format like 'Mon Jan 15, 2024'
    """
    now = datetime.now()
    return f"{now:%a} {now:%b} {now.day}, {now:%Y}"
