import os
import json
import sys
import inspect
import logging
import subprocess
import venv
import site
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.model import ToolExecResult

logger = logging.getLogger("astrbot")

@register("astrbot_plugin_tool_maker", "Gemini CLI", "AI自主进化引擎(Venv版)", "1.8.0")
class ToolMakerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 基础路径配置
        self.base_dir = os.path.dirname(__file__)
        self.tools_dir = os.path.join(self.base_dir, "tools")
        self.venv_dir = os.path.join(self.base_dir, "evolve_venv")
        
        if not os.path.exists(self.tools_dir): os.makedirs(self.tools_dir)
        
        # 初始化虚拟环境
        self._ensure_venv()
        
        self.dynamic_tools = {}
        self.load_saved_tools()

    def _ensure_venv(self):
        """确保虚拟环境存在并激活 site-packages"""
        if not os.path.exists(self.venv_dir):
            logger.info(f"正在创建插件私有虚拟环境: {self.venv_dir}")
            venv.create(self.venv_dir, with_pip=True)
        
        # 获取 venv 的 site-packages 路径
        if sys.platform == "win32":
            site_packages = os.path.join(self.venv_dir, "Lib", "site-packages")
        else:
            # 兼容不同版本的 Python 路径
            py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site_packages = os.path.join(self.venv_dir, "lib", py_ver, "site-packages")
        
        if os.path.exists(site_packages):
            # 动态加入当前进程的搜索路径
            site.addsitedir(site_packages)
            logger.info(f"已挂载私有依赖库: {site_packages}")
        
        self.venv_pip = os.path.join(self.venv_dir, "Scripts", "pip") if sys.platform == "win32" else os.path.join(self.venv_dir, "bin", "pip")

    def _install_deps(self, code: str):
        """分析并安装依赖到 venv"""
        import re
        pattern = re.compile(r'^\s*(?:from|import)\s+([a-zA-Z0-9_]+)', re.MULTILINE)
        modules = set(pattern.findall(code))
        std_libs = set(sys.builtin_module_names)
        
        to_install = []
        for module in modules:
            if module in std_libs or module == "astrbot": continue
            # 尝试导入，看是否已存在于当前路径（包括已挂载的 venv）
            try:
                __import__(module)
            except ImportError:
                to_install.append(module)
        
        if to_install:
            logger.info(f"正在安装依赖到虚拟环境: {to_install}")
            try:
                subprocess.check_call([self.venv_pip, "install", *to_install, "--quiet"])
                return True
            except Exception as e:
                logger.error(f"Pip 安装失败: {e}")
                return False
        return True

    @filter.llm_tool(name="evolute")
    async def evolute(self, event: AstrMessageEvent, tool_name: str, tool_description: str, python_code: str):
        """
        通过编写 Python 代码为自己进化新能力。
        代码运行在独立的 venv 虚拟环境中，支持自动安装依赖。
        
        Args:
            tool_name (string): 英文唯一标识。
            tool_description (string): 功能描述。
            python_code (string): 包含 'async def handler(args: dict)' 的代码。
        """
        try:
            # 安装依赖到私有 venv
            self._install_deps(python_code)
            
            # 语法检查
            compile(python_code, f"<evolve_{tool_name}>", "exec")
            
            data = {"name": tool_name, "description": tool_description, "code": python_code}
            with open(os.path.join(self.tools_dir, f"{tool_name}.json"), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            self.dynamic_tools[tool_name] = data
            return ToolExecResult(status=True, result=f"进化完成。依赖已安装至私有 venv。工具 '{tool_name}' 已就绪。")
        except Exception as e:
            return ToolExecResult(status=False, result=f"进化失败: {str(e)}")

    @filter.llm_tool(name="call_tool")
    async def call_tool(self, event: AstrMessageEvent, tool_name: str, args: dict = None):
        """执行已进化的工具"""
        if tool_name not in self.dynamic_tools:
            return ToolExecResult(status=False, result=f"未找到工具: {tool_name}")

        data = self.dynamic_tools[tool_name]
        try:
            runtime_ns = {"event": event, "context": self.context, "logger": logger, "__name__": "__main__"}
            exec(data['code'], globals(), runtime_ns)
            handler = runtime_ns.get('handler')
            
            if not handler: return ToolExecResult(status=False, result="未找到 handler 函数。")
            
            res = await handler(args or {}) if inspect.iscoroutinefunction(handler) else handler(args or {})
            return ToolExecResult(status=True, result=str(res))
        except Exception as e:
            return ToolExecResult(status=False, result=f"运行报错: {str(e)}")

    def load_saved_tools(self):
        for filename in os.listdir(self.tools_dir):
            if filename.endswith(".json"):
                with open(os.path.join(self.tools_dir, filename), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.dynamic_tools[data['name']] = data

    @filter.command("env_reset")
    async def env_reset(self, event: AstrMessageEvent):
        """管理指令：重置并清理虚拟环境"""
        import shutil
        if os.path.exists(self.venv_dir):
            shutil.rmtree(self.venv_dir)
        self._ensure_venv()
        yield event.plain_result("私有虚拟环境已重置。")

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
