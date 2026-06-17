from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core import AstrBotConfig


class OpenaiImage(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化"""
        logger.info("astrbot_plugin_openai_image initializing.")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass
