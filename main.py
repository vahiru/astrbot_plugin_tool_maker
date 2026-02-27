import os
import json
import inspect
import logging
from pydantic import Field
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.api.event import filter, AstrMessageEvent

logger = logging.getLogger("astrbot")

def create_dynamic_tool_instance(name, description, schema, code):
    """动态创建一个 FunctionTool 实例"""
    namespace = {}
    exec(code, namespace)
    handler = namespace.get('handler')
    
    if not handler:
        raise ValueError("代码中未找到 handler 函数")

    # 对于动态创建的工具，我们依然尝试使用简单的类定义
    class DynamicTool(FunctionTool):
        def __init__(self):
            # 手动设置属性，避免 Pydantic 构造函数验证
            self.name = name
            self.description = description
            self.parameters = schema

        async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
            try:
                if inspect.iscoroutinefunction(handler):
                    return await handler(context, **kwargs)
                else:
                    return handler(context, **kwargs)
            except Exception as e:
                return f"工具执行出错: {str(e)}"
    
    return DynamicTool()

class ToolMakerTool(FunctionTool):
    def __init__(self, plugin_instance):
        # 1. 显式初始化基类（如果不确定基类参数，先不传，或者传空）
        # 很多 Pydantic 基类不需要参数，或者参数是可选的
        try:
            super().__init__()
        except:
            pass
            
        # 2. 手动设置 Pydantic 预期的字段，绕过构造函数验证阶段
        self.name = "create_new_tool"
        self.description = "为机器人创建一个新的持久化工具。代码中必须包含 handler(context, **kwargs) 函数。"
        self.parameters = {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "工具的唯一标识名称（英文）"},
                "tool_description": {"type": "string", "description": "工具的功能描述"},
                "parameters_schema": {
                    "type": "object", 
                    "description": "工具参数的 JSON Schema。"
                },
                "python_code": {
                    "type": "string", 
                    "description": "包含 handler(context, **kwargs) 函数定义的 Python 代码。"
                }
            },
            "required": ["tool_name", "tool_description", "parameters_schema", "python_code"]
        }
        self.plugin = plugin_instance

    async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
        name = kwargs.get("tool_name")
        description = kwargs.get("tool_description")
        schema = kwargs.get("parameters_schema")
        code = kwargs.get("python_code")
        
        try:
            tool_instance = create_dynamic_tool_instance(name, description, schema, code)
            
            data = {
                "name": name,
                "description": description,
                "parameters": schema,
                "code": code
            }
            filepath = os.path.join(self.plugin.tools_dir, f"{name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            self.plugin.context.add_llm_tools(tool_instance)
            
            return f"成功创建并注册工具：{name}。"
        except Exception as e:
            logger.error(f"创建工具失败: {e}", exc_info=True)
            return f"创建工具失败: {str(e)}"

@register("astrbot_plugin_tool_maker", "Gemini CLI", "自动工具编写插件", "1.1.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
            
        # 使用手动定义的工具类
        self.context.add_llm_tools(ToolMakerTool(self))
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
                        tool_instance = create_dynamic_tool_instance(
                            data['name'], data['description'], data['parameters'], data['code']
                        )
                        self.context.add_llm_tools(tool_instance)
                        count += 1
                except Exception as e:
                    logger.error(f"加载动态工具 {filename} 失败: {e}")
        if count > 0:
            logger.info(f"已加载 {count} 个动态工具")

    @filter.command("tools")
    async def list_tools(self, event: AstrMessageEvent):
        """列出所有动态创建的工具"""
        tools = []
        if os.path.exists(self.tools_dir):
            for filename in os.listdir(self.tools_dir):
                if filename.endswith(".json"):
                    tools.append(filename[:-5])
        if not tools:
            yield event.plain_result("当前没有动态创建的工具。")
        else:
            yield event.plain_result("动态工具列表：\n" + "\n".join(tools))

    @filter.command("deltool")
    async def delete_tool(self, event: AstrMessageEvent, name: str):
        """删除一个动态创建的工具"""
        filepath = os.path.join(self.tools_dir, f"{name}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            yield event.plain_result(f"工具 {name} 已删除。重启后生效。")
        else:
            yield event.plain_result(f"未找到工具 {name}。")
