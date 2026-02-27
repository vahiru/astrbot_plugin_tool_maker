import os
import json
import sys
import inspect
import logging
import subprocess
import importlib.util
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.model import ToolExecResult

logger = logging.getLogger("astrbot")

def ensure_dependencies(code: str):
    """
    分析代码中的 import 语句并尝试安装缺失的依赖。
    """
    import re
    # 简单的正则匹配 import 语句
    pattern = re.compile(r'^\s*(?:from|import)\s+([a-zA-Z0-9_]+)', re.MULTILINE)
    modules = set(pattern.findall(code))
    
    # 排除内置模块
    std_libs = set(sys.builtin_module_names)
    
    for module in modules:
        if module in std_libs:
            continue
        # 检查模块是否已安装
        if importlib.util.find_spec(module) is None:
            logger.info(f"正在为动态工具安装缺失的依赖: {module}")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", module, "--quiet"])
            except Exception as e:
                logger.error(f"安装依赖 {module} 失败: {e}")

@register("astrbot_plugin_tool_maker", "Gemini CLI", "AI自主进化引擎", "1.7.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
        
        self.dynamic_tools = {}
        self.load_saved_tools()

    def load_saved_tools(self):
        if not os.path.exists(self.tools_dir):
            return
        count = 0
        for filename in os.listdir(self.tools_dir):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(self.tools_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.dynamic_tools[data['name']] = data
                        count += 1
                except Exception as e:
                    logger.error(f"加载工具配置 {filename} 失败: {e}")
        if count > 0:
            logger.info(f"已加载 {count} 个动态工具配置")

    @filter.llm_tool(name="evolute")
    async def evolute(self, event: AstrMessageEvent, tool_name: str, tool_description: str, python_code: str):
        """
        通过编写 Python 代码为自己创建并注册一个持久化的新能力（工具）。
        
        Args:
            tool_name (string): 工具的英文唯一标识名，例如 'web_searcher'。
            tool_description (string): 详细描述工具的功能、参数含义及返回格式。
        python_code (string): 完整的 Python 脚本。
          规范：
          1. 必须包含 'async def handler(args: dict)' 异步函数作为入口。
          2. 代码应自包含所有必要的 import 语句（缺失的库会自动尝试安装）。
          3. 可以使用全局变量 'context' (AstrBot Context) 和 'event' (当前消息事件)。
          4. 示例：
             import httpx
             async def handler(args):
                 url = args.get('url')
                 async with httpx.AsyncClient() as client:
                     resp = await client.get(url)
                     return resp.text
        """
        try:
            # 1. 自动处理依赖
            ensure_dependencies(python_code)
            
            # 2. 语法验证
            compile(python_code, f"<dynamic_tool_{tool_name}>", "exec")
            
            # 3. 持久化存储
            data = {
                "name": tool_name,
                "description": tool_description,
                "code": python_code
            }
            filepath = os.path.join(self.tools_dir, f"{tool_name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            self.dynamic_tools[tool_name] = data
            
            return ToolExecResult(status=True, result=f"进化成功！工具 '{tool_name}' 已就绪。你可以随时通过 call_tool 调用它。")
        except Exception as e:
            logger.error(f"进化工具失败: {e}", exc_info=True)
            return ToolExecResult(status=False, result=f"进化失败: {str(e)}")

    @filter.llm_tool(name="call_tool")
    async def call_tool(self, event: AstrMessageEvent, tool_name: str, args: dict = None):
        """
        调用你之前通过 evolute 定义的工具。
        
        Args:
            tool_name (string): 要调用的工具名称。
            args (dict): 传递给工具 handler 的参数字典。
        """
        if tool_name not in self.dynamic_tools:
            return ToolExecResult(status=False, result=f"未找到工具: {tool_name}")

        data = self.dynamic_tools[tool_name]
        try:
            # 构建运行时命名空间
            runtime_ns = {
                "event": event,
                "context": self.context,
                "logger": logger,
                "__name__": "__main__"
            }
            
            # 执行代码以获取 handler
            exec(data['code'], runtime_ns)
            handler = runtime_ns.get('handler')
            
            if not handler:
                return ToolExecResult(status=False, result=f"工具 '{tool_name}' 代码中未找到 handler 函数。")

            actual_args = args if args else {}
            
            if inspect.iscoroutinefunction(handler):
                result = await handler(actual_args)
            else:
                result = handler(actual_args)
            
            return ToolExecResult(status=True, result=str(result))
        except Exception as e:
            logger.error(f"执行动态工具 '{tool_name}' 出错: {e}", exc_info=True)
            return ToolExecResult(status=False, result=f"运行出错: {str(e)}")

    @filter.command("tools")
    async def list_tools_cmd(self, event: AstrMessageEvent):
        """管理指令：列出所有已进化的能力"""
        if not self.dynamic_tools:
            yield event.plain_result("目前还没有进化的能力。")
            return
        
        msg = "🚀 已进化的能力列表：\n"
        for name, data in self.dynamic_tools.items():
            msg += f"- {name}: {data['description']}\n"
        yield event.plain_result(msg)

    @filter.command("deltool")
    async def delete_tool_cmd(self, event: AstrMessageEvent, name: str):
        """管理指令：删除一个能力"""
        filepath = os.path.join(self.tools_dir, f"{name}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            self.dynamic_tools.pop(name, None)
            yield event.plain_result(f"能力 '{name}' 已从进化序列中移除。")
        else:
            yield event.plain_result(f"未找到能力: {name}")
