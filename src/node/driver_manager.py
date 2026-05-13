"""
pre Node Driver Manager — 加载 / 管理 driver 实例
"""
from __future__ import annotations
import asyncio
import importlib
import os
from typing import Dict


class DriverManager:
    """加载/管理多个 driver. driver type 名跟模块路径对应:
       cli-claude-code-local → drivers.cli_claude_code_local
       chrome-gemini → drivers.chrome_gemini"""

    def __init__(self, node_ctx: dict):
        self.node_ctx = node_ctx
        self.drivers: Dict[str, object] = {}

    @staticmethod
    def _module_name(type_name: str) -> str:
        return "drivers." + type_name.replace("-", "_")

    async def load(self, type_names: list[str]):
        for t in type_names:
            modname = self._module_name(t)
            try:
                mod = importlib.import_module(modname)
            except ImportError as e:
                print(f"[driver_manager] cannot load {t} ({modname}): {e}", flush=True)
                continue
            driver = getattr(mod, "DRIVER", None)
            if driver is None:
                print(f"[driver_manager] {modname} 没有导出 DRIVER", flush=True)
                continue
            try:
                await driver.init(self.node_ctx)
                self.drivers[t] = driver
                print(f"[driver_manager] loaded driver: {t}", flush=True)
            except Exception as e:
                print(f"[driver_manager] init failed for {t}: {e}", flush=True)

    async def discover_all_agents(self) -> list[dict]:
        """枚举所有 driver 的 agent, 返回供 master 注册的 dict 列表.

        state 从 metadata.status 派生 (master register_agent 会用此值):
          - metadata.status=="failed" → state="failed" (GUI 标红/标坏)
          - 其他 → state="idle" (默认; 后续 activity 上报会覆盖为 busy/blocked_user 等)
        """
        out = []
        for t, drv in self.drivers.items():
            try:
                specs = await drv.discover_agents()
                for s in specs:
                    md = s.metadata or {}
                    state = "failed" if md.get("status") == "failed" else "idle"
                    out.append({
                        "agent_id": s.agent_id,
                        "driver_type": t,
                        "role": s.role,
                        "capabilities": s.capabilities,
                        "metadata": md,
                        "state": state,
                    })
            except Exception as e:
                print(f"[driver_manager] {t}.discover failed: {e}", flush=True)
        return out

    async def send(self, agent_id: str, driver_type: str, message: dict) -> bool:
        drv = self.drivers.get(driver_type)
        if not drv:
            return False
        try:
            return await drv.send(agent_id, message)
        except Exception as e:
            print(f"[driver_manager] {driver_type}.send failed: {e}", flush=True)
            return False

    async def get_state(self, agent_id: str, driver_type: str) -> str:
        drv = self.drivers.get(driver_type)
        if not drv:
            return "unknown"
        try:
            return await drv.get_state(agent_id)
        except Exception:
            return "unknown"

    async def list_all_pending(self) -> list[dict]:
        """聚合所有 driver 检测出的 pending agent"""
        out = []
        for t, drv in self.drivers.items():
            try:
                specs = await drv.discover_agents()
            except Exception:
                continue
            for s in specs:
                try:
                    p = await drv.detect_pending(s.agent_id)
                except Exception as e:
                    print(f"[driver_manager] {t}.detect_pending {s.agent_id} failed: {e}", flush=True)
                    continue
                if p:
                    p["driver_type"] = t
                    out.append(p)
        return out

    async def decide(self, agent_id: str, driver_type: str, key: str) -> bool:
        drv = self.drivers.get(driver_type)
        if not drv:
            return False
        try:
            return await drv.decide(agent_id, key)
        except Exception as e:
            print(f"[driver_manager] {driver_type}.decide failed: {e}", flush=True)
            return False

    async def list_all_activity(self) -> list[dict]:
        """聚合所有 driver 检测出的 agent 活动状态"""
        out = []
        for t, drv in self.drivers.items():
            try:
                specs = await drv.discover_agents()
            except Exception:
                continue
            for s in specs:
                try:
                    a = await drv.detect_activity(s.agent_id)
                except Exception as e:
                    print(f"[driver_manager] {t}.detect_activity {s.agent_id} failed: {e}", flush=True)
                    continue
                if a:
                    a["driver_type"] = t
                    out.append(a)
        return out

    async def shutdown(self):
        for t, drv in list(self.drivers.items()):
            try:
                await drv.shutdown()
            except Exception:
                pass
        self.drivers.clear()
