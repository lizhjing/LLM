"""日期与时间相关工具。"""

from datetime import datetime
from typing import Any

from .registry import ToolRegistry, ToolSpec


def get_date(_: dict[str, Any]) -> str:
    """获取当前系统日期时间(本地时区,含英文星期)。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def is_leap_year(arguments: dict[str, Any]) -> str:
    """判断公历闰年:四年一闰,百年不闰,四百年再闰。"""
    try:
        year = int(arguments["year"])
    except (KeyError, TypeError, ValueError):
        return "参数错误:year 必须是整数"
    if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0:
        return f"{year} 年是闰年"
    return f"{year} 年不是闰年"


def days_between(arguments: dict[str, Any]) -> str:
    """计算两个日期之间的自然天数,日期格式 YYYY-MM-DD。"""
    try:
        d1 = datetime.strptime(arguments["date1"], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return "格式错误:date1 必须为 YYYY-MM-DD,且是真实存在的日期"
    try:
        d2 = datetime.strptime(arguments["date2"], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return "格式错误:date2 必须为 YYYY-MM-DD,且是真实存在的日期"
    diff = abs((d2 - d1).days)
    return f"{d1} 和 {d2} 之间相隔 {diff} 天"


def register(registry: ToolRegistry) -> None:
    """注册全部日期时间工具。"""
    registry.register(
        ToolSpec(
            name="getDate",
            description="获取当前电脑的日期和时间",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=get_date,
        )
    )
    registry.register(
        ToolSpec(
            name="isLeapYear",
            description="判断某个年份是否是闰年(公历)。规则:四年一闰,百年不闰,四百年再闰。",
            parameters={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "要判断的年份,如 2024"}
                },
                "required": ["year"],
            },
            handler=is_leap_year,
        )
    )
    registry.register(
        ToolSpec(
            name="daysBetween",
            description="计算两个日期之间相隔的自然天数。日期格式必须为 YYYY-MM-DD,如 2026-10-19。返回值为天数绝对值,不区分先后顺序。",
            parameters={
                "type": "object",
                "properties": {
                    "date1": {
                        "type": "string",
                        "description": "第一个日期,格式 YYYY-MM-DD,如 2026-10-19",
                    },
                    "date2": {
                        "type": "string",
                        "description": "第二个日期,格式 YYYY-MM-DD,如 2026-12-25",
                    },
                },
                "required": ["date1", "date2"],
            },
            handler=days_between,
        )
    )
