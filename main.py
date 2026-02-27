import os
import json
import inspect
import logging
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.model import ToolExecResult

logger = logging.getLogger("astrbot")

def create_dynamic_tool_instance(name, description, schema, code):
    """动态创建一个工具实例"""
    namespace = {}
    # 在命名空间中预注入一些常用库
    namespace.update({
        "os": os,
        "json": json,
        "logger": logger
    })
    
    try:
        exec(code, namespace)
    except Exception as e:
        raise ValueError(f"代码语法错误: {str(e)}")
        
    handler = namespace.get('handler')
    if not handler:
        raise ValueError("代码中未找到 handler(args) 函数")

    # 我们定义一个兼容接口，而不是直接继承 FunctionTool 避开框架扫描
    class DynamicToolProxy:
        def __init__(self):
            self.name = name
            self.description = description
            self.parameters = schema

        async def call(self, *args, **kwargs):
            # 处理 AstrBot 可能传入的 context 参数
            # 兼容 handler(args) 或 handler(context, **kwargs)
            sig = inspect.signature(handler)
            try:
                if len(sig.parameters) >= 2:
                    # 假设是 handler(context, **kwargs)
                    res = await handler(*args, **kwargs) if inspect.iscoroutinefunction(handler) else handler(*args, **kwargs)
                else:
                    # 假设是 handler(args)
                    res = await handler(kwargs) if inspect.iscoroutinefunction(handler) else handler(kwargs)
                return res
            except Exception as e:
                return f"工具执行出错: {str(e)}"
    
    return DynamicToolProxy()

@register("astrbot_plugin_tool_maker", "Gemini CLI", "AI能力制造引擎", "1.6.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        if not os.path.exists(self.tools_dir):
            os.makedirs(self.tools_dir)
        
        # 记录动态工具
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
                        # 这里我们只记录，实际调用通过代理执行
                        self.dynamic_tools[data['name']] = data
                        count += 1
                except Exception as e:
                    logger.error(f"加载工具配置 {filename} 失败: {e}")
        if count > 0:
            logger.info(f"已加载 {count} 个动态工具配置")

    @filter.llm_tool(name="define_tool")
    async def define_tool(self, event: AstrMessageEvent, name: str, description: str, python_code: str):
        """
        为自己定义一个新的持久化工具（能力）。
        
        Args:
            name (string): 工具的英文唯一标识名。
            description (string): 详细描述工具的功能和参数。
            python_code (string): Python 代码。必须包含 async def handler(args: dict) 函数。可以通过 args.get('param_name') 获取参数。
        """
        try:
            # 语法检查
            compile(python_code, "<string>", "exec")
            
            data = {
                "name": name,
                "description": description,
                "code": python_code
            }
            filepath = os.path.join(self.tools_dir, f"{name}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            self.dynamic_tools[name] = data
            return ToolExecResult(status=True, result=f"工具 '{name}' 定义成功。现在你可以直接通过 call_tool(name='{name}', args=...) 来调用它。")
        except Exception as e:
            return ToolExecResult(status=False, result=f"定义失败: {str(e)}")

    @filter.llm_tool(name="call_tool")
    async def call_tool(self, event: AstrMessageEvent, name: str, args: dict = None):
        """
        执行你之前定义的工具。
        
        Args:
            name (string): 要调用的工具名称。
            args (dict): 传递给工具的参数字典。
        """
        if name not in self.dynamic_tools:
            return ToolExecResult(status=False, result=f"未找到工具: {name}")

        data = self.dynamic_tools[name]
        try:
            local_ns = {"event": event, "context": self.context}
            exec(data['code'], globals(), local_ns)
            handler = local_ns.get('handler')
            
            if not handler:
                return ToolExecResult(status=False, result="错误: 代码中未找到 handler 函数。")

            actual_args = args if args else {}
            if inspect.iscoroutinefunction(handler):
                result = await handler(actual_args)
            else:
                result = handler(actual_args)
            
            return ToolExecResult(status=True, result=str(result))
        except Exception as e:
            return ToolExecResult(status=False, result=f"运行出错: {str(e)}")

    @filter.llm_tool(name="run_python_repl")
    async def run_python_repl(self, event: AstrMessageEvent, code: str):
        """
        即时执行一段 Python 代码并获取结果。适用于临时计算或单次任务。
        
        Args:
            code (string): 要执行的 Python 代码。如果需要返回结果，请将结果赋值给变量 'result'。
        """
        try:
            local_ns = {"event": event, "context": self.context}
            exec(code, globals(), local_ns)
            res = local_ns.get('result', "执行完成，无 result 返回值。")
            return ToolExecResult(status=True, result=str(res))
        except Exception as e:
            return ToolExecResult(status=False, result=f"执行失败: {str(e)}")

    @filter.command("tools")
    async def list_tools_cmd(self, event: AstrMessageEvent):
        """列出所有动态定义的工具"""
        if not self.dynamic_tools:
            yield event.plain_result("目前还没有定义的工具。")
            return
        
        msg = "已定义的能力列表：\n"
        for name, data in self.dynamic_tools.items():
            msg += f"- {name}: {data['description']}\n"
        yield event.plain_result(msg)

    @filter.command("deltool")
    async def delete_tool_cmd(self, event: AstrMessageEvent, name: str):
        """删除一个能力"""
        filepath = os.path.join(self.tools_dir, f"{name}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            self.dynamic_tools.pop(name, None)
            yield event.plain_result(f"能力 '{name}' 已删除。")
        else:
            yield event.plain_result(f"未找到能力: {name}")
