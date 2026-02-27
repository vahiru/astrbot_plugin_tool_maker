import os
import json
import sys
import inspect
import logging
import subprocess
import importlib.util
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper

logger = logging.getLogger("astrbot")

def get_dynamic_tool_class(name, description, schema, code, plugin_instance):
    """
    在函数内部定义类，彻底躲避框架的启动扫描。
    """
    @dataclass
    class DynamicEvolvedTool(FunctionTool):
        name: str = name
        description: str = description
        parameters: dict = Field(default_factory=lambda: schema)

        async def call(self, context: ContextWrapper, **kwargs) -> ToolExecResult:
            # 运行时动态执行代码
            try:
                # 注入上下文
                runtime_ns = {
                    "context": context,
                    "plugin": plugin_instance,
                    "logger": logger,
                    "__name__": f"dynamic_tool_{self.name}"
                }
                # 预执行代码以加载环境
                exec(code, runtime_ns)
                handler = runtime_ns.get('handler')
                if not handler:
                    return ToolExecResult(status=False, result="未找到 handler 函数。")
                
                # 执行逻辑
                if inspect.iscoroutinefunction(handler):
                    res = await handler(**kwargs)
                else:
                    res = handler(**kwargs)
                return ToolExecResult(status=True, result=str(res))
            except Exception as e:
                logger.error(f"工具 {self.name} 执行失败: {e}", exc_info=True)
                return ToolExecResult(status=False, result=f"执行出错: {str(e)}")
    
    return DynamicEvolvedTool()

@register("astrbot_plugin_tool_maker", "Gemini CLI", "Evolute Engine", "2.0.0")
class EvoluteEngine(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_dir = os.path.dirname(__file__)
        self.tools_dir = os.path.join(self.base_dir, "evolutions")
        if not os.path.exists(self.tools_dir): os.makedirs(self.tools_dir)
        
        # 尝试使用 uv 提升性能 (Python 界的 Bun)
        self.use_uv = self._check_uv()
        self.load_saved_tools()

    def _check_uv(self):
        try:
            subprocess.run(["uv", "--version"], capture_output=True)
            return True
        except:
            return False

    def _sync_deps(self, code: str):
        """同步代码中声明的依赖"""
        import re
        # 支持 PEP 723 风格或简单的 import 识别
        deps = re.findall(r'import\s+([a-zA-Z0-9_]+)', code)
        # 简单过滤掉内置和框架库
        to_install = [d for d in set(deps) if d not in sys.builtin_module_names and d != "astrbot"]
        
        if to_install:
            cmd = ["uv", "pip", "install"] if self.use_uv else [sys.executable, "-m", "pip", "install"]
            try:
                subprocess.check_call(cmd + to_install + ["--quiet"])
                logger.info(f"依赖同步完成: {to_install}")
            except Exception as e:
                logger.error(f"依赖同步失败: {e}")

    @filter.llm_tool(name="evolute")
    async def evolute(self, event: AstrMessageEvent, tool_name: str, tool_description: str, parameters_schema: dict, python_code: str):
        """
        [进化能力] 通过编写 Python 代码为自己创造一个全新的、持久化的工具。
        进化后的工具将直接出现在你的工具列表中，你可以像调用原生工具一样调用它。
        
        规范：
        1. 必须包含 'async def handler(**kwargs)'。
        2. 代码中应包含所需的 import。
        3. parameters_schema 需符合 JSON Schema 规范。
        """
        try:
            # 1. 同步依赖
            self._sync_deps(python_code)
            
            # 2. 验证与持久化
            data = {
                "name": tool_name,
                "description": tool_description,
                "parameters": parameters_schema,
                "code": python_code
            }
            with open(os.path.join(self.tools_dir, f"{tool_name}.json"), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 3. 实时注册为 LLM 工具 (一级公民)
            tool_inst = get_dynamic_tool_class(tool_name, tool_description, parameters_schema, python_code, self)
            self.context.add_llm_tools(tool_inst)
            
            return ToolExecResult(status=True, result=f"进化成功！能力 '{tool_name}' 已实时植入你的神经中枢。现在你可以直接调用它了。")
        except Exception as e:
            return ToolExecResult(status=False, result=f"进化失败: {str(e)}")

    def load_saved_tools(self):
        """启动时加载所有已进化的能力"""
        if not os.path.exists(self.tools_dir): return
        for filename in os.listdir(self.tools_dir):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(self.tools_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    tool_inst = get_dynamic_tool_class(data['name'], data['description'], data['parameters'], data['code'], self)
                    self.context.add_llm_tools(tool_inst)
                except Exception as e:
                    logger.error(f"加载进化能力 {filename} 失败: {e}")

    @filter.command("tools")
    async def list_evolutions(self, event: AstrMessageEvent):
        """查看当前已进化的所有能力"""
        tools = [f[:-5] for f in os.listdir(self.tools_dir) if f.endswith(".json")]
        if not tools: yield event.plain_result("暂无已进化的能力。")
        else: yield event.plain_result("🧬 已进化能力：\n" + "\n".join(tools))
