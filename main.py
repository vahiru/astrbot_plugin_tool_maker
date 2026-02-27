import os
import json
import inspect
import logging
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_tool_maker", "Gemini CLI", "自动工具编写插件", "1.5.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
        
        # 内部命名空间，用于持久化运行时的函数
        self.namespace = {
            "context": context,
            "logger": logger
        }

    # 使用指令和 Action 双重身份
    # AI 可以通过 Function Calling 调用这些方法
    
    @filter.command("define_tool")
    async def define_tool(self, event: AstrMessageEvent, name: str, description: str, code: str):
        """
        为自己定义一个新的持久化工具。
        参数:
        name (string): 工具名称（英文）
        description (string): 工具功能描述
        code (string): Python 代码，必须包含 handler(args_dict) 函数。
        """
        try:
            # 简单验证代码语法
            compile(code, "<string>", "exec")
            
            data = {
                "name": name,
                "description": description,
                "code": code
            }
            filepath = os.path.join(self.tools_dir, f"{name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            yield event.plain_result(f"工具 '{name}' 已保存。你可以通过 call_tool 来使用它。")
        except Exception as e:
            yield event.plain_result(f"定义失败: {str(e)}")

    @filter.command("call_tool")
    async def call_tool(self, event: AstrMessageEvent, name: str, args: dict = None):
        """
        调用你之前定义过的工具。
        参数:
        name (string): 工具名称
        args (dict): 传递给工具 handler 的参数字典
        """
        filepath = os.path.join(self.tools_dir, f"{name}.json")
        if not os.path.exists(filepath):
            yield event.plain_result(f"未找到工具: {name}")
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 动态执行代码
            local_ns = {}
            exec(data['code'], self.namespace, local_ns)
            handler = local_ns.get('handler')
            
            if not handler:
                yield event.plain_result(f"错误: 工具 '{name}' 中未找到 handler 函数。")
                return

            # 执行逻辑
            if args is None: args = {}
            if inspect.iscoroutinefunction(handler):
                result = await handler(args)
            else:
                result = handler(args)
            
            yield event.plain_result(f"工具 '{name}' 运行结果: \n{result}")
        except Exception as e:
            yield event.plain_result(f"工具运行出错: {str(e)}")

    @filter.command("run_python")
    async def run_python(self, event: AstrMessageEvent, code: str):
        """
        直接运行一段 Python 代码并返回结果（REPL）。
        """
        try:
            # 捕获 print 输出或返回最后一行表达式的值
            # 这里简化处理，直接执行并捕获异常
            local_ns = {}
            exec(code, self.namespace, local_ns)
            # 如果定义了 result 变量，则返回它
            res = local_ns.get('result', "执行成功（无返回结果）")
            yield event.plain_result(f"代码运行结果: {res}")
        except Exception as e:
            yield event.plain_result(f"运行失败: {str(e)}")

    @filter.command("tools")
    async def list_tools(self, event: AstrMessageEvent):
        """列出所有已定义的工具"""
        tools = []
        if os.path.exists(self.tools_dir):
            for filename in os.listdir(self.tools_dir):
                if filename.endswith(".json"):
                    with open(os.path.join(self.tools_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        tools.append(f"- {data['name']}: {data['description']}")
        
        if not tools:
            yield event.plain_result("目前还没有定义的工具。")
        else:
            yield event.plain_result("已定义的能力列表：\n" + "\n".join(tools))

    @filter.command("deltool")
    async def delete_tool(self, event: AstrMessageEvent, name: str):
        """删除一个能力"""
        filepath = os.path.join(self.tools_dir, f"{name}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            yield event.plain_result(f"能力 '{name}' 已删除。")
        else:
            yield event.plain_result(f"未找到能力: {name}")
