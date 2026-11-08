"""插件本地的输出关键词过滤，不注册影响其他插件的全局消息钩子。"""

from functools import wraps


def normalize_keywords(keywords):
    """面板和发送逻辑共用：去首尾空白、去空项，按原顺序去重。"""
    if isinstance(keywords, str):
        keywords = keywords.splitlines()
    if not isinstance(keywords, (list, tuple)):
        return []
    return list(dict.fromkeys(keyword.strip() for keyword in keywords
                              if isinstance(keyword, str) and keyword.strip()))


def blocked_keyword(config, *texts):
    """按字面子串匹配；缺省屏蔽「无限」，显式空清单关闭过滤。"""
    for keyword in normalize_keywords(config.get("output_block_keywords", ["无限"])):
        if any(keyword in text for text in texts if isinstance(text, str)):
            return keyword
    return None


def message_text(message):
    """读取 AstrBot 消息链的文字，连同同一链上的图片一起决定是否发送。"""
    if isinstance(message, str):
        return message
    chain = message if isinstance(message, (list, tuple)) else getattr(message, "chain", [])
    return "".join(getattr(part, "text", "") or "" for part in chain)


def filter_output(handler):
    """只包装本插件的回复入口，保留异步生成器及命令签名。"""
    @wraps(handler)
    async def wrapped(self, event, *args, **kwargs):
        async for result in handler(self, event, *args, **kwargs):
            if result is not None and not self._output_blocked(message_text(result)):
                yield result

    return wrapped
