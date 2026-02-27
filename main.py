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
    """动态创建一个 FunctionTool 实例"""
    namespace = {}
    # 在命名空间中执行代码，以获取定义的函数和导入
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
    name: str = "create_new_tool"
    description: str = "为机器人创建一个新的持久化工具。你需要提供工具名称、描述、JSON Schema 参数定义以及 Python 代码。代码中必须包含 handler(context, **kwargs) 函数。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "工具的唯一标识名称（英文）"},
                "tool_description": {"type": "string", "description": "工具的功能描述"},
                "parameters_schema": {
                    "type": "object", 
                    "description": "工具参数的 JSON Schema。例如: {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}"
                },
                "python_code": {
                    "type": "string", 
                    "description": "包含 handler(context, **kwargs) 函数定义的 Python 代码。可以包含 import 语句。"
                }
            },
            "required": ["tool_name", "tool_description", "parameters_schema", "python_code"]
        }
    )
    
    def __init__(self, plugin_instance):
        self.plugin = plugin_instance

    async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
        name = kwargs.get("tool_name")
        description = kwargs.get("tool_description")
        schema = kwargs.get("parameters_schema")
        code = kwargs.get("python_code")
        
        try:
            # 尝试创建实例以验证代码
            tool_instance = create_dynamic_tool_instance(name, description, schema, code)
            
            # 保存到文件
            data = {
                "name": name,
                "description": description,
                "parameters": schema,
                "code": code
            }
            filepath = os.path.join(self.plugin.tools_dir, f"{name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 立即注册到当前上下文
            self.plugin.context.add_llm_tools(tool_instance)
            
            return f"成功创建并注册工具：{name}。现在你可以直接使用它了。"
        except Exception as e:
            return f"创建工具失败: {str(e)}"

@register("astrbot_plugin_tool_maker", "Gemini CLI", "自动工具编写插件", "1.0.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
            
        # 注册工具制造者工具
        self.context.add_llm_tools(ToolMakerTool(self))
            
        # 加载已保存的工具
        self.load_saved_tools()

    def load_saved_tools(self):
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
