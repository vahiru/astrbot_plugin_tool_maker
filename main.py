import os
import json
import inspect
import logging
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.api.event import filter, AstrMessageEvent

logger = logging.getLogger("astrbot")

def create_dynamic_tool_instance(name, description, schema, code):
    """动态创建一个工具实例"""
    namespace = {}
    exec(code, namespace)
    handler = namespace.get('handler')
    
    if not handler:
        raise ValueError("代码中未找到 handler 函数")

    @dataclass
    class DynamicTool(FunctionTool):
        name: str = name
        description: str = description
        parameters: dict = Field(default_factory=lambda: schema)

        async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
            try:
                if inspect.iscoroutinefunction(handler):
                    return await handler(context, **kwargs)
                else:
                    return handler(context, **kwargs)
            except Exception as e:
                return f"工具执行出错: {str(e)}"
    
    return DynamicTool()

@dataclass
class ToolMakerTool(FunctionTool):
    # 严格遵循 Pydantic Dataclass 规范，不定义自定义 __init__
    name: str = "create_new_tool"
    description: str = "为机器人创建一个新的持久化工具。代码中必须包含 handler(context, **kwargs) 函数。"
    parameters: dict = Field(
        default_factory=lambda: {
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
    )

    async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
        # 动态从 context 中获取插件实例
        plugin = None
        agent_context = context.context # 这通常是 AstrAgentContext
        if hasattr(agent_context, 'star_instances'):
            # 尝试通过名字获取插件实例
            plugin = agent_context.star_instances.get("astrbot_plugin_tool_maker")
        
        if not plugin:
            return "错误：无法在当前上下文中定位 ToolMaker 插件实例。请确保插件已正确加载。"

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
            # 使用插件实例中的 tools_dir
            filepath = os.path.join(plugin.tools_dir, f"{name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            plugin.context.add_llm_tools(tool_instance)
            
            return f"成功创建并注册工具：{name}。"
        except Exception as e:
            logger.error(f"创建工具失败: {e}", exc_info=True)
            return f"创建工具失败: {str(e)}"

@register("astrbot_plugin_tool_maker", "Gemini CLI", "自动工具编写插件", "1.3.1")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
            
        # 实例化时不传递任何参数，完全避开 Pydantic 验证错误
        self.context.add_llm_tools(ToolMakerTool())
            
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
